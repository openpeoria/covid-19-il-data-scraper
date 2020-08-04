# -*- coding: utf-8 -*-
"""
    app.helpers
    ~~~~~~~~~~~

    Provides misc helper functions
"""
import pdb

from inspect import getmembers, isclass
from os import getenv
from traceback import print_exception, format_exc
from json.decoder import JSONDecodeError
from logging import Formatter

import pygogo as gogo
import config

from pygogo.formatters import DATEFMT
from flask import current_app as app, has_request_context, request
from config import Config, __APP_NAME__

ADMIN = Config.ADMIN
MAILGUN_DOMAIN = Config.MAILGUN_DOMAIN
MAILGUN_SMTP_PASSWORD = Config.MAILGUN_SMTP_PASSWORD

hkwargs = {"subject": f"{__APP_NAME__} notification", "recipients": [ADMIN.email]}

if MAILGUN_DOMAIN and MAILGUN_SMTP_PASSWORD:
    # NOTE: Sandbox domains are restricted to authorized recipients only.
    # https://help.mailgun.com/hc/en-us/articles/217531258
    mkwargs = {
        "host": getenv("MAILGUN_SMTP_SERVER", "smtp.mailgun.org"),
        "port": getenv("MAILGUN_SMTP_PORT", 587),
        "sender": f"notifications@{MAILGUN_DOMAIN}",
        "username": getenv("MAILGUN_SMTP_LOGIN", f"postmaster@{MAILGUN_DOMAIN}"),
        "password": MAILGUN_SMTP_PASSWORD,
    }

    hkwargs.update(mkwargs)

email_hdlr = gogo.handlers.email_hdlr(**hkwargs)


def configure(flask_config, **kwargs):
    if kwargs.get("config_file"):
        flask_config.from_pyfile(kwargs["config_file"])
    elif kwargs.get("config_envvar"):
        flask_config.from_envvar(kwargs["config_envvar"])
    elif kwargs.get("config_mode"):
        obj = getattr(config, kwargs["config_mode"])
        flask_config.from_object(obj)
    else:
        flask_config.from_envvar("APP_SETTINGS", silent=True)


def get_member(module, member_name, classes_only=True):
    predicate = isclass if classes_only else None

    for member in getmembers(module, predicate):
        if member[0].lower() == member_name.lower():
            return member[1]


def log(message=None, ok=True, r=None, exit_on_completion=False, **kwargs):
    if r is not None:
        ok = r.ok

        try:
            message = r.json().get("message")
        except JSONDecodeError:
            message = r.text

    if message and ok:
        app.logger.info(message)
    elif message:
        try:
            app.logger.error(message)
        except ConnectionRefusedError:
            app.logger.info("Connect refused. Make sure an SMTP server is running.")
            app.logger.info("Try running `sudo postfix start`.")
            app.logger.info(message)

    if exit_on_completion:
        exit(0 if ok else 1)


def exception_hook(etype, value=None, tb=None, debug=False, callback=None, **kwargs):
    if debug:
        print_exception(etype, value, tb)
        pdb.post_mortem(tb)
    else:
        message = format_exc() if kwargs.get("use_tb") else etype
        log(message, ok=False)

    callback() if callback else None


class RequestFormatter(Formatter):
    def format(self, record):
        record.url = request.url if has_request_context() else None
        return super().format(record)


flask_formatter = RequestFormatter(
    "[%(levelname)s %(asctime)s] via %(url)s in %(module)s:%(lineno)s: %(message)s",
    datefmt=DATEFMT,
)
