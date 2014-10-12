# Copyright 2013 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
"""I handle vdsm's configuration life cycle.

This is achieved by utilizing modules from configurators package to:
- Configure the machine to run vdsm after package installation.
- Cleanup configuration before package removal.
- Check configuration status and validity upon init.

configurators interface is described below.
"""


from collections import deque
import argparse
import sys
import traceback

from . import \
    service, \
    expose, \
    UsageError, \
    requiresRoot
from .configurators import \
    certificates, \
    YES, \
    InvalidConfig, \
    InvalidRun, \
    libvirt, \
    multipath, \
    NO, \
    sanlock, \
    sebool


_CONFIGURATORS = dict((m.name, m) for m in (
    certificates,
    libvirt,
    sanlock,
    sebool,
    multipath,
))


#
# Configurators Interface:
# The only required attribute for modules is 'name'.
# Default implementation of all other attributes/methods follows;
#

def _getrequires(module):
    """Return a set of module names required by this module.

    Those modules will be included even if not provided in --module.
    """
    return getattr(module, 'requires', frozenset())


def _getservices(module):
    """Return the names of services to reload.

    These services will be stopped before this configurator is called,
    and will be started in reversed order when the configurator is done.
    """
    return getattr(module, 'services', ())


def _validate(module):
    """Return True if this module's configuration is valid.

    Note: Returning False will cause vdsm to abort during initialization.
    """
    return getattr(module, 'validate', lambda: True)()


def _configure(module):
    """Prepare this module to run vdsm."""
    getattr(module, 'configure', lambda: None)()


def _isconfigured(module):
    """Return state of configuration. See configurators/__init__.py

    Note: returning NO will cause vdsm to abort during initialization.

    Note: after configure isconfigured should return MAYBE or YES.
    """
    return getattr(module, 'isconfigured', lambda: NO)()


def _removeConf(module):
    """Cleanup vdsm's configuration."""
    getattr(module, 'removeConf', lambda: None)()


@expose("configure")
@requiresRoot
def configure(*args):
    """
    configure [-h|...]
    Configure external services for vdsm
    Invoke with -h for complete usage.
    """
    args = _parse_args(*args)
    configurer_to_trigger = []

    sys.stdout.write("\nChecking configuration status...\n\n")
    for c in args.modules:
        isconfigured = _isconfigured(c)
        override = args.force and isconfigured != YES
        if not override and not _validate(c):
            raise InvalidConfig(
                "Configuration of %s is invalid" % c.name
            )
        if override or isconfigured == NO:
            configurer_to_trigger.append(c)

    services = []
    for c in configurer_to_trigger:
        for s in _getservices(c):
            if service.service_status(s, False) == 0:
                if not args.force:
                    raise InvalidRun(
                        "\n\nCannot configure while service '%s' is "
                        "running.\n Stop the service manually or use the "
                        "--force flag.\n" % s
                    )
                services.append(s)

    for s in services:
        service.service_stop(s)

    sys.stdout.write("\nRunning configure...\n")
    for c in configurer_to_trigger:
        _configure(c)
        sys.stdout.write("Reconfiguration of %s is done.\n" % (c.name,))

    for s in reversed(services):
        service.service_start(s)
    sys.stdout.write("\nDone configuring modules to VDSM.\n")


@expose("is-configured")
@requiresRoot
def isconfigured(*args):
    """
    is-configured [-h|...]
    Determine if module is configured
    Invoke with -h for complete usage.
    """
    ret = True
    args = _parse_args(*args)

    m = [c.name for c in args.modules if _isconfigured(c) == NO]

    if m:
        sys.stdout.write(
            "Modules %s are not configured\n " % ', '.join(m),
        )
        ret = False

    if not ret:
        msg = \
            """

One of the modules is not configured to work with VDSM.
To configure the module use the following:
'vdsm-tool configure [--module module-name]'.

If all modules are not configured try to use:
'vdsm-tool configure --force'
(The force flag will stop the module's service and start it
afterwards automatically to load the new configuration.)
"""
        raise InvalidRun(msg)


@expose("validate-config")
@requiresRoot
def validate_config(*args):
    """
    validate-config [-h|...]
    Determine if configuration is valid
    Invoke with -h for complete usage.
    """
    ret = True
    args = _parse_args(*args)

    m = [c.name for c in args.modules if not _validate(c)]

    if m:
        sys.stdout.write(
            "Modules %s contains invalid configuration\n " % ', '.join(m),
        )
        ret = False

    if not ret:
        raise InvalidConfig("Config is not valid. Check conf files")


@expose("remove-config")
@requiresRoot
def remove_config(*args):
    """
    Remove vdsm configuration from conf files
    """
    args = _parse_args(*args)
    failed = False
    for c in args.modules:
        try:
            _removeConf(c)
            sys.stdout.write(
                "removed configuration of module %s successfully\n" %
                c.name
            )

        except Exception:
            sys.stderr.write(
                "can't remove configuration of module %s\n" %
                c.name
            )
            traceback.print_exc(file=sys.stderr)
            failed = True
    if failed:
        raise InvalidRun("Remove configuration failed")


def _add_dependencies(modulesNames):
    queue = deque(modulesNames)
    retNames = set(queue)

    while queue:
        next_ = queue.popleft()
        try:
            requiredNames = _getrequires(_CONFIGURATORS[next_])
        except KeyError:
            available = ', '.join(sorted(_CONFIGURATORS))
            raise UsageError(
                "error: argument --module: invalid choice: %s\n"
                "(available: %s)\n" % (next_, available)
            )

        for requiredName in requiredNames:
            if requiredName not in retNames:
                retNames.add(requiredName)
                queue.append(requiredName)

    return retNames


def _sort_modules(modulesNames):
    # Greedy topological sort algorithm(Dijkstra).
    # At each step go over all tasks and find a task that can be executed
    # before all others. If at any point there is none, there is a circle!
    # Note: there's an improved performance variant, but this is good enough.
    modulesNames = set(modulesNames)
    sortedModules = []
    while modulesNames:

        for c in modulesNames:
            if _getrequires(_CONFIGURATORS[c]).issubset(set(sortedModules)):
                modulesNames.remove(c)
                sortedModules.append(c)
                break

        else:
            raise RuntimeError("Dependency circle found!")

    return sortedModules


def _parse_args(action, *args):
    parser = argparse.ArgumentParser('vdsm-tool %s' % (action))
    parser.add_argument(
        '--module',
        dest='modules',
        default=[],
        metavar='STRING',
        action='append',
        help=(
            'Specify the module to run the action on '
            '(e.g %s).\n'
            'If non is specified, operation will run for '
            'all related modules.'
            % _CONFIGURATORS.keys()
        ),
    )
    if action == "configure":
        parser.add_argument(
            '--force',
            dest='force',
            default=False,
            action='store_true',
            help='Force configuration, trigger services restart',
        )

    args = parser.parse_args(args)
    if not args.modules:
        args.modules = _CONFIGURATORS.keys()

    args.modules = _sort_modules(_add_dependencies(args.modules))

    args.modules = [_CONFIGURATORS[cName] for cName in args.modules]

    return args
