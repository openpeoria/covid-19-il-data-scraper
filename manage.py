#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim: sw=4:ts=4:expandtab

""" A script to manage development tasks """
from os import path as p
from subprocess import call, check_call, CalledProcessError
from urllib.parse import urlsplit
from datetime import datetime as dt, timedelta

import pygogo as gogo

from flask import current_app as app
from flask_script import Manager

from config import Config
from app import create_app
from app.api import add_report, load_report, remove_report, get_status
from app.utils import TODAY
from app.helpers import log, exception_hook

BASEDIR = p.dirname(__file__)
DEF_PORT = 5000
DATE_FORMAT = Config.S3_DATE_FORMAT
DAYS = Config.DAYS

manager = Manager(create_app)
manager.add_option("-m", "--cfgmode", dest="config_mode", default="Development")
manager.add_option("-f", "--cfgfile", dest="config_file", type=p.abspath)
manager.main = manager.run  # Needed to do `manage <command>` from the cli

logger = gogo.Gogo(__name__).logger


@manager.option("-h", "--host", help="The server host")
@manager.option("-p", "--port", help="The server port", default=DEF_PORT)
@manager.option("-t", "--threaded", help="Run multiple threads", action="store_true")
def serve(port, **kwargs):
    """Runs the flask development server"""
    with app.app_context():
        kwargs["threaded"] = kwargs.get("threaded", app.config["PARALLEL"])
        kwargs["debug"] = app.config["DEBUG"]

        if app.config.get("SERVER_NAME"):
            parsed = urlsplit(app.config["SERVER_NAME"])
            host, port = parsed.netloc, parsed.port or port
        else:
            host = app.config["HOST"]

        kwargs.setdefault("host", host)
        kwargs.setdefault("port", port)
        app.run(**kwargs)


runserver = serve


@manager.command
def check():
    """Check staged changes for lint errors"""
    exit(call(p.join(BASEDIR, "helpers", "check-stage")))


@manager.command
def checkstage():
    """Checks staged with git pre-commit hook"""

    path = p.join(p.dirname(__file__), "app", "tests", "test.sh")
    cmd = "sh %s" % path
    return call(cmd, shell=True)


@manager.option("-w", "--where", help="Requirement file", default=None)
def test(where):
    """Run nose tests"""
    cmd = "nosetests -xvw %s" % where if where else "nosetests -xv"
    return call(cmd, shell=True)


@manager.option("-w", "--where", help="Modules to check")
def prettify(where):
    """Prettify code with black"""
    def_where = ["app", "manage.py", "config.py"]
    extra = where.split(" ") if where else def_where

    try:
        check_call(["black"] + extra)
    except CalledProcessError as e:
        exit(e.returncode)


@manager.option("-w", "--where", help="Modules to check")
@manager.option("-s", "--strict", help="Check with pylint", action="store_true")
def lint(where, strict):
    """Check style with linters"""
    def_where = ["app", "tests", "manage.py", "config.py"]
    extra = where.split(" ") if where else def_where

    args = ["pylint", "--rcfile=tests/standard.rc", "-rn", "-fparseable", "app"]

    try:
        check_call(["flake8"] + extra)
        check_call(args) if strict else None
    except CalledProcessError as e:
        exit(e.returncode)


@manager.option("-r", "--remote", help="the heroku branch", default="staging")
def add_keys(remote):
    """Deploy staging app"""
    cmd = "heroku keys:add ~/.ssh/id_rsa.pub --remote {}"
    check_call(cmd.format(remote).split(" "))


@manager.option("-r", "--remote", help="the heroku branch", default="staging")
def deploy(remote):
    """Deploy staging app"""
    branch = "master" if remote == "production" else "features"
    cmd = "git push origin {}"
    check_call(cmd.format(branch).split(" "))


@manager.command
def require():
    """Create requirements.txt"""
    cmd = "pip freeze -l | grep -vxFf dev-requirements.txt "
    cmd += "| grep -vxFf requirements.txt "
    cmd += "> base-requirements.txt"
    call(cmd.split(" "))


@manager.option(
    "-d", "--end", help="the report ending date", default=TODAY.strftime(DATE_FORMAT)
)
@manager.option(
    "-n",
    "--days",
    help="the number of historical days to fetch from start",
    type=int,
    default=DAYS,
)
@manager.option(
    "-s",
    "--source",
    help="source data location",
    default="idph",
    choices=["idph", "local", "s3", "ckan"],
)
@manager.option(
    "-t",
    "--dest",
    help="dest file location",
    default="local",
    choices=["idph", "local", "s3", "ckan"],
)
@manager.option(
    "-r",
    "--report-type",
    help="report type",
    default="county",
    choices=["county", "zip", "hospital"],
)
@manager.option("-e", "--enqueue", help="queue the work", action="store_true")
def add_reports(end, days, enqueue, **kwargs):
    """Upload reports or save to disk"""
    with app.app_context():
        end_date = dt.strptime(end, DATE_FORMAT)

        for day in range(days):
            start_date = end_date - timedelta(days=day)
            report_date = start_date.strftime(DATE_FORMAT)

            try:
                response = add_report(report_date, enqueue=enqueue, **kwargs)
            except Exception as e:
                exception_hook(e.__class__.__name__, debug=app.debug, use_tb=True)
            else:
                log(**response)


@manager.option(
    "-d", "--end", help="the report ending date", default=TODAY.strftime(DATE_FORMAT)
)
@manager.option(
    "-n",
    "--days",
    help="the number of historical days to fetch from start",
    type=int,
    default=DAYS,
)
@manager.option(
    "-s",
    "--source",
    help="source data location",
    default="idph",
    choices=["idph", "local", "s3", "ckan"],
)
@manager.option(
    "-r",
    "--report-type",
    help="report type",
    default="county",
    choices=["county", "zip", "hospital"],
)
@manager.option("-e", "--enqueue", help="queue the work", action="store_true")
def remove_reports(end, days, enqueue, **kwargs):
    """Delete reports"""
    with app.app_context():
        end_date = dt.strptime(end, DATE_FORMAT)

        for day in range(days):
            start_date = end_date - timedelta(days=day)
            report_date = start_date.strftime(DATE_FORMAT)

            try:
                response = remove_report(report_date, enqueue=enqueue, **kwargs)
            except Exception as e:
                exception_hook(e.__class__.__name__, debug=app.debug, use_tb=True)
            else:
                log(**response)


@manager.option(
    "-d", "--end", help="the report ending date", default=TODAY.strftime(DATE_FORMAT)
)
@manager.option(
    "-n",
    "--days",
    help="the number of historical days to fetch from start",
    type=int,
    default=DAYS,
)
@manager.option(
    "-r",
    "--report-type",
    help="report type",
    default="county",
    choices=["county", "zip", "hospital"],
)
@manager.option("-e", "--enqueue", help="queue the work", action="store_true")
def load_reports(end, days, enqueue, **kwargs):
    """Fetch reports to return time series"""
    with app.app_context():
        end_date = dt.strptime(end, DATE_FORMAT)

        for day in range(days):
            start_date = end_date - timedelta(days=day)
            report_date = start_date.strftime(DATE_FORMAT)

            try:
                response = load_report(report_date, enqueue=enqueue, **kwargs)
            except Exception as e:
                exception_hook(e.__class__.__name__, debug=app.debug, use_tb=True)
            else:
                log(**response)


@manager.option(
    "-s",
    "--source",
    help="source data location",
    default="local",
    choices=["idph", "local", "s3", "ckan"],
)
@manager.option(
    "-r",
    "--report-type",
    help="report type",
    default="county",
    choices=["county", "zip", "hospital"],
)
def status(source, **kwargs):
    """Get IDPH reports status"""
    with app.app_context():
        use_s3 = source == "s3"

        try:
            response = get_status(use_s3=use_s3, **kwargs)
        except Exception as e:
            exception_hook(e.__class__.__name__, debug=app.debug, use_tb=True)
        else:
            log(**response)


@manager.command
def work():
    """Run the rq-worker"""
    call("python -u worker.py", shell=True)


if __name__ == "__main__":
    manager.run()
