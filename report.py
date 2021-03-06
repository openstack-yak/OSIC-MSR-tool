#!/usr/bin/env python
### process summary reports from stackalytics & garret output

# python-2 compatibility:
from __future__ import print_function
import sys

import argparse
import os
import json
import yaml

from datetime import datetime, timedelta
from time import sleep

import configparser
from jinja2 import Environment, PackageLoader, FileSystemLoader

import requests

# report.py

# Python-2 compatibility: need to add unicode check:
STRING_CHECK = (str, unicode) if sys.version_info.major<3 else (str)
yaml_dump = yaml.safe_dump if sys.version_info.major<3 else yaml.dump


__version__ = '0.1'

# stackalytics.com is way faster than stackalytics.o.o
LYTICS_API = 'http://stackalytics.com/api/1.0/activity'
GARRET_API = 'https://review.openstack.org/changes/'

# default
config_paths = [ ".", "~/.config/MSR"]


def main():
    # id, users, field, Templates = read_config()
    cfg = read_config()

    actions = []
    wip = []
    # for i in analytics_files:
    for analytic, pending in analytics_sources(cfg):
        actions.extend(process_analytic(analytic, cfg['Fields']))
        wip.extend(process_pending(pending, cfg['Gerrit']))

    summary = {}
    for i in actions:
        t = i['type']
        summary[t] = 1+ summary.setdefault(t, 0)

    summary["wip"] = len(wip)

    ###
    # This is run in the template renderer:
    ##
    # TODO:  move to a function; option type (txt, html, docx)
    jinjaenv = Environment(loader=FileSystemLoader(cfg['Templates']['path']))
    template = jinjaenv.get_template(cfg['Templates']['template'])
    print(template.render(summary=summary, field=cfg['Fields'], gerrit_fields=cfg['Gerrit'], actions=actions, wip=wip))


def read_config(file='msr.ini'):
    '''
    reads a config file,
    Returns:
    the config, as a by-section dictionary
    '''

    fn = file_in_paths(file, config_paths)
    if not fn:
        raise RuntimeError("msr.ini file not found; expected in one of: {}".format(', '.join(config_paths)))
    # global id, users, fields
    config = configparser.ConfigParser(allow_no_value=True)
    config.read(file)

    cfg = {}
    for i in config.sections():
        if i == 'Users':
            cfg.update({i: list(config[i])})
        else:
            cfg.update({i: dict(config[i].items())})

    # generically make a list of "path" elements in any section
    #  because we like multiline path specs.
    for sect in config.sections():
        if config.has_option(sect, 'path'):
            t = cfg[sect]
            t['path'] = t['path'].split('\n')

    return cfg


def default_dates():
    '''
    by default, return start/end for previous month.
    '''
    # this month:
    start = datetime.utcnow()\
            .replace(day=1,
                     hour=0, minute=0, second=0, microsecond=0)
    # previous month:
    end = start - timedelta(days=1)  # last day
    start = end.replace(day=1)  # first day
    return (start, end)


def get_data_and_filename(usr, api_base, params,
                          data_path, file_name_extension,
                          start, end):
    '''
    given:
      - a user
      - a api base url
      - a function to create the user filename
      - the report-
      - ...

    find their data file, and:
      - update if appropriate
      - return the updated file path
    '''

    _file_name = lambda u: "{}.{}.{}-{}.{}".format(
                            start.year,
                            start.month,
                            start.day,
                            (end-start+timedelta(days=1)).days,
                            u)

    file_name = lambda u: _file_name(u) + file_name_extension

    report_path = os.path.join(start.strftime("%Y"), start.strftime("%b"))

    fpth = file_in_paths('.', data_path)
    if fpth:
        fpth_report_path = os.path.join(fpth, report_path)
        # doing it this way for python-2 compatibility:
        if not os.path.exists(fpth_report_path):
            os.makedirs(fpth_report_path)
    else:
        raise FileNotFoundError("None of your configured data paths exist;")


    data_file_subpath = os.path.join(report_path, file_name(usr))

    data_fn = file_in_paths(data_file_subpath, data_path)
    if not data_fn:
        #   form the destination, if necessary,
        #     from the first base in the path
        #   form api uri;
        #   hit api
        if isinstance(params, STRING_CHECK):
            for n in range(5):
                try:
                    s = requests.get(api_base + params)
                except (requests.exceptions.Timeout,
                        requests.exceptions.HTTPError,
                        requests.exceptions.URLRequired) as e:
                    sleep(3)
                else:
                    break
            s.raise_for_status()
            # review api returns cruft/marker at the front:
            payload = json.loads(s.text.split('\n', 1)[1])
            for i in payload:
                i['owner'].update({'gerrit_id': usr})
            # gerrit_id = None
        else:   # need to put a delay timer in these
            for n in range(5):
                try:
                    s = requests.get(api_base, params=params)
                except (requests.exceptions.Timeout,
                        requests.exceptions.HTTPError,
                        requests.exceptions.URLRequired) as e:
                    sleep(3)
                else:
                    break
            s.raise_for_status()
            payload = s.json()

        data_fn = os.path.join(file_in_paths(report_path, data_path),
                          file_name(usr))
        #   store datafile into path;
        with open(data_fn, 'w') as f:
            yaml_dump(payload, f, default_flow_style=False)

    return data_fn


def analytics_sources(cfg, start=None, end=None):
    '''
    based on options, get source for the time period requested:
    - from file cache, if it exists
        and not forcing an update;
    - from the api sources;
    - save to file;
    '''
    # set default start end dates to all of previous month:
    if start == None and end == None:
        start, end = default_dates()
    data_path = cfg['Data']['path']
    # for each of the users in the project:
    for usr in cfg['Users']:
        params = {'start_date': epoch_value(start),
                  'end_date':   epoch_value(end),
                  'gerrit_id': usr}
        data_fn = get_data_and_filename(usr, LYTICS_API, params,
                                        data_path, ".yaml", start, end)
        params = "?q=status:open+owner:" + usr
        open_fn = get_data_and_filename(usr, GARRET_API, params,
                                        data_path, ".open.yaml", start, end)
        yield (data_fn, open_fn)



def process_analytic(data_file, fields):

    with open(data_file) as f:
        # useful for json; not needed for yaml;
        # consume_commentline(f)
        id_activity = yaml.load(f)

    actions = []
    for i in id_activity['activity']:
        # take required field aliases;
        # update w/ configured fields
        if 'project' in fields:
            key = fields['project']
            if 'sandbox' in i.get(key, None):
                continue
        act = {}
        for j in fields:
            # check for fields[j] == None
            key = fields[j]
            val = i.get(key, None)
            act.update({j: val})
        actions.append(act)
    return actions


def process_pending(data_file, fields):

    with open(data_file) as f:
        # useful for json; not needed for yaml;
        # consume_commentline(f)
        id_activity = yaml.load(f)

    actions = []
    for i in id_activity:
        # take required field aliases;
        # update w/ configured fields
        if 'project' in fields:
            key = fields['project']
            if 'sandbox' in i.get(key, None):
                continue
        act = {}
        for j in fields:
            # check for fields[j] == None
            key = fields[j]
            val = i.get(key, None)
            act.update({j: val})
        actions.append(act)
    return actions


def consume_commentline(f):
    # check if the first line starts with a comment
    commented = f.read(80).lstrip().startswith('#')
    f.seek(0)
    if commented:
        _ = f.readline()


# TODO:  get a date range - if none, then get last month:
#  1. get a month date (or today);
#  2. last day of month = next month:first-day - timedelta(1 day)

epoch_dt = datetime(1970,1,1)
epoch_value = lambda i: int((i - epoch_dt).total_seconds())

def epoch_range(start, end):
    ''' datetime:  start, end
        return:  a pair of values suitable for giving to stackalytics API
    '''
    return epoch_value(start), epoch_value(end)


def file_in_paths(file, paths):
    for path in paths:
        this = os.path.join(os.path.expanduser(path), file)
        # if os.path.isfile(this):
        if os.path.exists(this):
            return os.path.abspath(this)
    return None


# TODO:
def process_options():
    class MyArgParser(argparse.ArgumentParser):
        def error(self, message):
            sys.stderr.write('error: {}\n'.format(message))
            self.print_help()
            sys.exit(2)

    description = '''
    Produce a summary monthly status report.
    Default if for the previous month.
    Default group members and format is in msr.ini, searched for first in the current directory,
      then in ~/.config/MSR.
    The default output format is described by the jinja2 template as defined
      in the config Templates section (name and paths).
    Some output formats possible are text, html, or docx (Word).

    '''

    parser = MyArgParser(description=description)

if __name__ == "__main__":
    # eventually, parse args
    main()

