#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys

from os import path as p
from dotenv import load_dotenv

import pkutils

try:
    from setuptools import setup, find_packages
except ImportError:
    from distutils.core import setup, find_packages

PARENT_DIR = p.abspath(p.dirname(__file__))

load_dotenv(p.join(PARENT_DIR, '.env'))
sys.dont_write_bytecode = True
requirements = list(pkutils.parse_requirements('base-requirements.txt'))
dev_requirements = list(pkutils.parse_requirements('dev-requirements.txt'))
_prod_requirements = set(pkutils.parse_requirements('requirements.txt'))
prod_requirements = list(_prod_requirements.difference(requirements))
readme = pkutils.read('README.rst')
module = pkutils.parse_module(p.join(PARENT_DIR, 'app', '__init__.py'))
license = module.__license__
version = module.__version__
project = module.__package_name__
description = module.__description__
user = config.__USER__

setup_require = [r for r in dev_requirements if 'pkutils' in r]

setup(
    name=project,
    version=version,
    description=description,
    long_description=readme,
    author=module.__author__,
    author_email=module.__email__,
    url=pkutils.get_url(project, user),
    download_url=pkutils.get_dl_url(project, user, version),
    packages=find_packages(exclude=['docs', 'tests']),
    include_package_data=True,
    package_data={
        'helpers': ['helpers/*'],
        'tests': ['tests/*'],
        'data': ['data/*'],
    },
    install_requires=requirements,
    extras_require={
        'develop': dev_requirements,
        'production': prod_requirements,
    },
    setup_requires=setup_require,
    dependency_links=dependencies,
    tests_require=dev_requirements,
    license=license,
    zip_safe=False,
    keywords=[project] + description.split(' '),
    package_data={},
    classifiers=[
        pkutils.LICENSES[license],
        pkutils.get_status(version),
        'Natural Language :: English',
        'Programming Language :: Python :: 3',
        'Framework :: Flask',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'Programming Language :: Python :: 3.3',
        'Environment :: Web Environment',
        'Intended Audience :: Developers',
        'Operating System :: MacOS :: MacOS X',
        'Operating System :: Microsoft :: Windows',
        'Operating System :: POSIX',
    ],
    platforms=['MacOS X', 'Windows', 'Linux'],
)
