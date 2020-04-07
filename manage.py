#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim: sw=4:ts=4:expandtab

""" A script to manage development tasks """
from os import path as p, getenv
from subprocess import call, check_call, CalledProcessError
from urllib.parse import urlsplit
from datetime import datetime as dt, timedelta

import pygogo as gogo

from flask import current_app as app
from flask_script import Manager
from config import Config, __APP_NAME__, __AUTHOR_EMAIL__
from app import create_app, cache
from app.api import fetch_report, load_report, get_status
from app.utils import TODAY, YESTERDAY

BASEDIR = p.dirname(__file__)
DEF_PORT = 5000
DATE_FORMAT = Config.S3_DATE_FORMAT
DAYS = Config.DAYS

manager = Manager(create_app)
manager.add_option("-m", "--cfgmode", dest="config_mode", default="Development")
manager.add_option("-f", "--cfgfile", dest="config_file", type=p.abspath)
manager.main = manager.run  # Needed to do `manage <command>` from the cli

hdlr_kwargs = {
    "subject": f"{__APP_NAME__} notification",
    "recipients": [__AUTHOR_EMAIL__],
}

if getenv("MAILGUN_SMTP_PASSWORD"):
    # NOTE: Sandbox domains are restricted to authorized recipients only.
    def_username = f"postmaster@{getenv('MAILGUN_DOMAIN')}"
    mailgun_kwargs = {
        "host": getenv("MAILGUN_SMTP_SERVER", "smtp.mailgun.org"),
        "port": getenv("MAILGUN_SMTP_PORT", 587),
        "sender": f"notifications@{getenv('MAILGUN_DOMAIN')}",
        "username": getenv("MAILGUN_SMTP_LOGIN", def_username),
        "password": getenv("MAILGUN_SMTP_PASSWORD"),
    }

    hdlr_kwargs.update(mailgun_kwargs)

high_hdlr = gogo.handlers.email_hdlr(**hdlr_kwargs)
logger = gogo.Gogo(__name__, high_hdlr=high_hdlr).logger


def log(message=None, ok=True, r=None, **kwargs):
    if r:
        try:
            message = r.json().get("message")
        except JSONDecodeError:
            message = r.text

    if message and ok:
        logger.info(message)
    elif message:
        logger.error(message)


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
@manager.option("-s", "--use_s3", help="save to AWS S3", action="store_true")
@manager.option("-e", "--enqueue", help="queue the work", action="store_true")
def fetch_reports(end, days, use_s3, enqueue):
    """Fetch IDPH reports save to disk"""
    with app.app_context():
        end_date = dt.strptime(end, DATE_FORMAT)

        for day in range(days):
            start_date = end_date - timedelta(days=day)
            report_date = start_date.strftime(DATE_FORMAT)
            response = fetch_report(report_date, use_s3=use_s3, enqueue=enqueue)
            logger.debug(response)


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
@manager.option("-e", "--enqueue", help="queue the work", action="store_true")
def load_reports(end, days, use_s3, enqueue):
    """Fetch s3 reports return time series"""
    with app.app_context():
        end_date = dt.strptime(end, DATE_FORMAT)

        for day in range(days):
            start_date = end_date - timedelta(days=day)
            report_date = start_date.strftime(DATE_FORMAT)
            response = load_report(report_date, enqueue=enqueue)
            logger.debug(response)


@manager.option("-s", "--use_s3", help="save to AWS S3", action="store_true")
def status(use_s3):
    """Fetch IDPH reports save to disk"""
    with app.app_context():
        response = get_status(use_s3=use_s3)
        log(**response)


@manager.command
def work():
    """Run the rq-worker"""
    call("python -u worker.py", shell=True)


if __name__ == "__main__":
    manager.run()
