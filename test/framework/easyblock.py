##
# Copyright 2012-2014 Ghent University
#
# This file is part of EasyBuild,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://vscentrum.be/nl/en),
# the Hercules foundation (http://www.herculesstichting.be/in_English)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# http://github.com/hpcugent/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
##
"""
Unit tests for easyblock.py

@author: Jens Timmerman (Ghent University)
@author: Kenneth Hoste (Ghent University)
"""

import os
import re
import tempfile
import sys
from test.framework.utilities import EnhancedTestCase
from unittest import TestLoader, main

from easybuild.framework.easyblock import EasyBlock
from easybuild.framework.easyconfig.easyconfig import EasyConfig
from easybuild.tools import config
from easybuild.tools.build_log import EasyBuildError
from easybuild.tools.filetools import write_file
from easybuild.tools.module_generator import det_full_module_name


class EasyBlockTest(EnhancedTestCase):
    """ Baseclass for easyblock testcases """

    def writeEC(self):
        """ create temporary easyconfig file """
        write_file(self.eb_file, self.contents)

    def setUp(self):
        """ setup """
        super(EasyBlockTest, self).setUp()

        fd, self.eb_file = tempfile.mkstemp(prefix='easyblock_test_file_', suffix='.eb')
        os.close(fd)

        self.orig_tmp_logdir = os.environ.get('EASYBUILD_TMP_LOGDIR', None)
        self.test_tmp_logdir = tempfile.mkdtemp()
        os.environ['EASYBUILD_TMP_LOGDIR'] = self.test_tmp_logdir

    def test_empty(self):
        self.contents = "# empty"
        self.writeEC()
        """ empty files should not parse! """
        self.assertRaises(EasyBuildError, EasyBlock, self.eb_file)
        self.assertErrorRegex(EasyBuildError, "expected a valid path", EasyBlock, "")

    def test_easyblock(self):
        """ make sure easyconfigs defining extensions work"""
        name = "pi"
        version = "3.14"
        self.contents =  '\n'.join([
            'name = "%s"' % name,
            'version = "%s"' % version,
            'homepage = "http://example.com"',
            'description = "test easyconfig"',
            'toolchain = {"name":"dummy", "version": "dummy"}',
            'exts_list = ["ext1"]',
        ])
        self.writeEC()
        stdoutorig = sys.stdout
        sys.stdout = open("/dev/null", 'w')
        eb = EasyBlock(self.eb_file)
        self.assertEqual(eb.cfg['name'], name)
        self.assertEqual(eb.cfg['version'], version)
        self.assertRaises(NotImplementedError, eb.run_all_steps, True, False)
        sys.stdout.close()
        sys.stdout = stdoutorig

        # test passing of parsed easyconfig file to EasyBlock constructor
        ec = EasyConfig(self.eb_file)
        eb2 = EasyBlock(ec)
        self.assertEqual(eb2.cfg['name'], name)
        self.assertEqual(eb2.cfg['version'], version)

        # cleanup
        eb.close_log()
        os.remove(eb.logfile)

    def test_fake_module_load(self):
        """Testcase for fake module load"""
        self.contents = """
name = "pi"
version = "3.14"
homepage = "http://example.com"
description = "test easyconfig"
toolchain = {"name":"dummy", "version": "dummy"}
"""
        self.writeEC()
        eb = EasyBlock(self.eb_file)
        eb.installdir = config.build_path()
        fake_mod_data = eb.load_fake_module()
        eb.clean_up_fake_module(fake_mod_data)

        # cleanup
        eb.close_log()
        os.remove(eb.logfile)

    def test_extensions_step(self):
        """Test the extensions_step"""
        self.contents = """
name = "pi"
version = "3.14"
homepage = "http://example.com"
description = "test easyconfig"
toolchain = {"name":"dummy", "version": "dummy"}
exts_list = ['ext1']
"""
        self.writeEC()
        """Testcase for extensions"""
        # test for proper error message without the exts_defaultclass set
        eb = EasyBlock(self.eb_file)
        eb.installdir = config.install_path()
        self.assertRaises(EasyBuildError, eb.extensions_step)
        self.assertErrorRegex(EasyBuildError, "No default extension class set", eb.extensions_step)

        # test if everything works fine if set
        self.contents += "\nexts_defaultclass = ['easybuild.framework.extension', 'Extension']"
        self.writeEC()
        eb = EasyBlock(self.eb_file)
        eb.builddir = config.build_path()
        eb.installdir = config.install_path()
        eb.extensions_step()

        # test for proper error message when skip is set, but no exts_filter is set
        self.assertRaises(EasyBuildError, eb.skip_extensions)
        self.assertErrorRegex(EasyBuildError, "no exts_filter set", eb.skip_extensions)

        # cleanup
        eb.close_log()
        os.remove(eb.logfile)

    def test_skip_extensions_step(self):
        """Test the skip_extensions_step"""
        self.contents = """
name = "pi"
version = "3.14"
homepage = "http://example.com"
description = "test easyconfig"
toolchain = {"name":"dummy", "version": "dummy"}
exts_list = ['ext1', 'ext2']
exts_filter = ("if [ %(name)s == 'ext2' ]; then exit 0; else exit 1; fi", '')
exts_defaultclass = ['easybuild.framework.extension', 'Extension']
"""
        # check if skip skips correct extensions
        self.writeEC()
        eb = EasyBlock(self.eb_file)
        #self.assertTrue('ext1' in eb.exts.keys() and 'ext2' in eb.exts.keys())
        eb.builddir = config.build_path()
        eb.installdir = config.install_path()
        eb.skip = True
        eb.extensions_step()
        # 'ext1' should be in eb.exts
        self.assertTrue('ext1' in [y for x in eb.exts for y in x.values()])
        # 'ext2' should not
        self.assertFalse('ext2' in [y for x in eb.exts for y in x.values()])

        # cleanup
        eb.close_log()
        os.remove(eb.logfile)

    def test_make_module_step(self):
        """Test the make_module_step"""
        name = "pi"
        version = "3.14"
        modextravars = {'PI': '3.1415', 'FOO': 'bar'}
        modextrapaths = {'PATH': 'pibin', 'CPATH': 'pi/include'}
        self.contents = '\n'.join([
            'name = "%s"' % name,
            'version = "%s"' % version,
            'homepage = "http://example.com"',
            'description = "test easyconfig"',
            "toolchain = {'name': 'dummy', 'version': 'dummy'}",
            "dependencies = [('foo', '1.2.3')]",
            "builddependencies = [('bar', '9.8.7')]",
            "modextravars = %s" % str(modextravars),
            "modextrapaths = %s" % str(modextrapaths),
        ])

        # test if module is generated correctly
        self.writeEC()
        eb = EasyBlock(self.eb_file)
        eb.installdir = os.path.join(config.install_path(), 'pi', '3.14')
        modpath = os.path.join(eb.make_module_step(), name, version)
        self.assertTrue(os.path.exists(modpath))

        # verify contents of module
        f = open(modpath, 'r')
        txt = f.read()
        f.close()
        self.assertTrue(re.search("^#%Module", txt.split('\n')[0]))
        self.assertTrue(re.search("^conflict\s+%s$" % name, txt, re.M))
        self.assertTrue(re.search("^set\s+root\s+%s$" % eb.installdir, txt, re.M))
        self.assertTrue(re.search('^setenv\s+EBROOT%s\s+".root"\s*$' % name.upper(), txt, re.M))
        self.assertTrue(re.search('^setenv\s+EBVERSION%s\s+"%s"$' % (name.upper(), version), txt, re.M))
        for (key, val) in modextravars.items():
            self.assertTrue(re.search('^setenv\s+%s\s+"%s"$' % (key, val), txt, re.M))
        for (key, val) in modextrapaths.items():
            self.assertTrue(re.search('^prepend-path\s+%s\s+\$root/%s$' % (key, val), txt, re.M))

    def test_gen_dirs(self):
        """Test methods that generate/set build/install directory names."""
        self.contents =  '\n'.join([
            "name = 'pi'",
            "version = '3.14'",
            "homepage = 'http://example.com'",
            "description = 'test easyconfig'",
            "toolchain = {'name': 'dummy', 'version': 'dummy'}",
        ])
        self.writeEC()
        stdoutorig = sys.stdout
        sys.stdout = open("/dev/null", 'w')
        eb = EasyBlock(self.eb_file)
        resb = eb.gen_builddir()
        eb.mod_name = det_full_module_name(eb.cfg)  # required by gen_installdir()
        resi = eb.gen_installdir()
        eb.make_builddir()
        eb.make_installdir()
        # doesn't return anything
        self.assertEqual(resb, None)
        self.assertEqual(resi, None)
        # directories are set, and exist
        self.assertTrue(os.path.isdir(eb.builddir))
        self.assertTrue(os.path.isdir(eb.installdir))

        # make sure cleaning up old build dir is default
        self.assertTrue(eb.cfg['cleanupoldbuild'] or eb.cfg.get('cleanupoldbuild', True))
        builddir = eb.builddir
        eb.gen_builddir()
        self.assertEqual(builddir, eb.builddir)
        eb.cfg['cleanupoldbuild'] = True
        eb.gen_builddir()
        self.assertEqual(builddir, eb.builddir)

        # make sure build dir is unique
        eb.cfg['cleanupoldbuild'] = False
        builddir = eb.builddir
        for i in range(0,3):
            eb.gen_builddir()
            self.assertEqual(eb.builddir, "%s.%d" % (builddir, i))
            eb.make_builddir()

        # cleanup
        sys.stdout.close()
        sys.stdout = stdoutorig
        eb.close_log()

    def tearDown(self):
        """ make sure to remove the temporary file """
        super(EasyBlockTest, self).tearDown()

        os.remove(self.eb_file)
        if self.orig_tmp_logdir is not None:
            os.environ['EASYBUILD_TMP_LOGDIR'] = self.orig_tmp_logdir

def suite():
    """ return all the tests in this file """
    return TestLoader().loadTestsFromTestCase(EasyBlockTest)

if __name__ == '__main__':
    main()
