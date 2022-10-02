import sys
from .project import get_project
from .config import edit_projects 
import argparse

from getrel import project
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def _install(args):
    _do_install(args.projects, args.update)

def _upgrade(args):
    _do_install(args.projects, update=True)

def _do_install(project_names: list[str], update: bool = False):
    print(project_names)
    if not project_names:
        project_names = list(edit_projects())
    for project_name in project_names:
        try:
            project = get_project(project_name)
            logger.info('Installing %s', project)
            project.install()
        except Exception as e:
            logger.error('Failed to install %s: %s', project_name, e, exc_info=True)
            

def main():
    p = argparse.ArgumentParser(description="""
            Simple CLI for getrel. 

            Install using the 'tui' extra to get more and fancier commands 
        """)
    sub = p.add_subparsers()
    
    install = sub.add_parser('install')
    install.set_defaults(func=_install)
    install.add_argument('projects', nargs='*')
    install.add_argument('-u', '--update', type=bool, default=False)

    upgrade = sub.add_parser('upgrade')
    install.set_defaults(func=_upgrade)
    upgrade.add_argument('projects', nargs='*')

    o = p.parse_args()
    if hasattr(o, 'func'):
        o.func(o)
