#!/usr/bin/python2.4
#
# CDDL HEADER START
#
# The contents of this file are subject to the terms of the
# Common Development and Distribution License (the "License").
# You may not use this file except in compliance with the License.
#
# You can obtain a copy of the license at usr/src/OPENSOLARIS.LICENSE
# or http://www.opensolaris.org/os/licensing.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# When distributing Covered Code, include this CDDL HEADER in each
# file and include the License file at usr/src/OPENSOLARIS.LICENSE.
# If applicable, add the following below this CDDL HEADER, with the
# fields enclosed by brackets "[]" replaced with your own identifying
# information: Portions Copyright [yyyy] [name of copyright owner]
#
# CDDL HEADER END
#
# Copyright 2009 Sun Microsystems, Inc.  All rights reserved.
# Use is subject to license terms.

try:
        import cherrypy
except ImportError:
        # Optional dependency.
        pass

import bisect
import datetime
import errno
import os
import pkg.fmri as fmri
import pkg.indexer as indexer
import pkg.manifest as manifest
import pkg.pkgsubprocess as subprocess
import pkg.portable as portable
import pkg.search_errors as se
import pkg.server.query_parser as query_p
import pkg.version as version
import random
import re
import shutil
import signal
import stat
import sys
import tempfile
import threading
import urllib

from pkg.misc import EmptyI
from pkg.server.errors import SvrConfigError

class CatalogException(Exception):
        def __init__(self, args=None):
                self.args = args


class CatalogPermissionsException(CatalogException):
        """Used to indicate the server catalog files do not have the expected
        permissions."""

        def __init__(self, files):
                """files should contain a list object with each entry consisting
                of a tuple of filename, expected_mode, received_mode."""
                if not files:
                        files = []
                CatalogException.__init__(self, files)

        def __str__(self):
                msg = _("The following catalog files have incorrect "
                    "permissions:\n")
                for f in self.args:
                        fname, emode, fmode = f
                        msg += _("\t%(fname)s: expected mode: %(emode)s, found "
                            "mode: %(fmode)s\n") % ({ "fname": fname,
                            "emode": emode, "fmode": fmode })
                return msg


class ServerCatalog(object):
        """A Catalog is the representation of the package FMRIs available to
        the repository.

        The serialized structure of the repository is an unordered list of
        available package versions, followed by an unordered list of
        incorporation relationships between packages.  This latter section
        allows the graph to be topologically sorted by the client.

        S Last-Modified: [timespec]

        XXX A publisher mirror-uri ...
        XXX ...

        V fmri
        V fmri
        ...
        C fmri
        C fmri
        ...
        I fmri fmri
        I fmri fmri
        ...

        In order to improve the time to search the catalog, a cached list
        of package names is kept in the catalog instance."""

        # The file mode to be used for all catalog files.
        file_mode = stat.S_IRUSR|stat.S_IWUSR|stat.S_IRGRP|stat.S_IROTH

        # XXX Mirroring records also need to be allowed from client
        # configuration, and not just catalogs.
        #
        # XXX It would be nice to include available tags and package sizes,
        # although this could also be calculated from the set of manifests.
        #
        # XXX Current code is O(N_packages) O(M_versions), should be
        # O(1) O(M_versions), and possibly O(1) O(1).
        #
        # XXX Initial estimates suggest that the Catalog could be composed of
        # 1e5 - 1e7 lines.  Catalogs across these magnitudes will need to be
        # spread out into chunks, and may require a delta-oriented update
        # interface.

        def __init__(self, cat_root, publisher=None, pkg_root=None,
            read_only=False, index_root=None, repo_root=None,
            rebuild=False, verbose=False, fork_allowed=False,
            has_writable_root=False):
                """Create a catalog.  If the path supplied does not exist,
                this will create the required directory structure.
                Otherwise, if the directories are already in place, the
                existing catalog is opened.  If pkg_root is specified
                and no catalog is found at cat_root, the catalog will be
                rebuilt.  publisher names the publisher that
                is represented by this catalog."""

                self.fork_allowed = fork_allowed
                self.index_root = index_root
                self.repo_root = repo_root
                # XXX this is a cheap hack to determine whether information
                # about catalog operations should be logged using cherrypy.
                self.verbose = verbose

                # The update_handle lock protects the update_handle variable.
                # This allows update_handle to be checked and acted on in a
                # consistent step, preventing the dropping of needed updates.
                # The check at the top of refresh index should always be done
                # prior to deciding to spin off a process for indexing as it
                # prevents more than one indexing process being run at the same
                # time.
                self.searchdb_update_handle_lock = threading.Lock()

                if os.name == "posix" and self.fork_allowed:
                        try:
                                signal.signal(signal.SIGCHLD,
                                    self.child_handler)
                        except ValueError:
                                self.__log("Tried to create signal handler in "
                                    "a thread other than the main thread.")

                self.searchdb_update_handle = None
                self._search_available = False
                self.deferred_searchdb_updates = []
                self.deferred_searchdb_updates_lock = threading.Lock()

                self.refresh_again = False

                self.catalog_root = cat_root
                self.catalog_file = os.path.normpath(os.path.join(
                    self.catalog_root, "catalog"))
                self.attrs = {}
                self.pub = publisher
                self.pkg_root = pkg_root
                self.read_only = read_only
                self.__size = -1

                assert not (read_only and rebuild)

                # The catalog protects the catalog file from having multiple
                # threads writing to it at the same time.
                self.catalog_lock = threading.Lock()

                self.attrs["npkgs"] = 0

                if not os.path.exists(cat_root):
                        try:
                                os.makedirs(cat_root)
                        except EnvironmentError, e:
                                if e.errno in (errno.EACCES, errno.EROFS):
                                        return
                                raise

                # Rebuild catalog, if we're the depot and it's necessary.
                if pkg_root is not None and rebuild:
                        self.build_catalog()

                self.load_attrs()
                self.check_prefix()
                self.__set_perms()

                if self.repo_root:
                        searchdb_file = os.path.join(self.repo_root, "search")
                        try:
                                os.unlink(searchdb_file + ".pag")
                        except OSError:
                                pass
                        try:
                                os.unlink(searchdb_file + ".dir")
                        except OSError:
                                pass

                if not read_only or has_writable_root:
                        try:
                                try:
                                        self.refresh_index()
                                except se.InconsistentIndexException, e:
                                        s = _("Index corrupted or out of date. "
                                            "Removing old index directory (%s) "
                                            " and rebuilding search "
                                            "indexes.") % e.cause
                                        self.__log(s, "INDEX")
                                        shutil.rmtree(self.index_root)
                                        try:
                                                self.refresh_index()
                                        except se.IndexingException, e:
                                                self.__log(str(e), "INDEX")
                                except se.IndexingException, e:
                                        self.__log(str(e), "INDEX")
                        except EnvironmentError, e:
                                if e.errno == errno.EACCES:
                                        if has_writable_root:
                                                raise SvrConfigError(
                                                    _("writable root not "
                                                    "writable by current user "
                                                    "id or group."))
                                        else:
                                                raise SvrConfigError(
                                                    _("unable to write to "
                                                    "index directory."))
                                raise
                else:
                        self._check_search()

        @staticmethod
        def destroy(root=None):
                """Removes the on-disk files for the catalog only."""

                for fname in ("attrs", "catalog"):
                        path = os.path.normpath(os.path.join(root, fname))
                        try:
                                portable.remove(path)
                        except EnvironmentError, e:
                                if e.errno != errno.ENOENT:
                                        raise

        @staticmethod
        def whence(cmd):
                if cmd[0] != '/':
                        tmp_cmd = cmd
                        cmd = None
                        path = os.environ['PATH'].split(':')
                        path.append(os.environ['PWD'])
                        for p in path:
                                if os.path.exists(os.path.join(p, tmp_cmd)):
                                        cmd = os.path.join(p, tmp_cmd)
                                        break
                        assert cmd
                return cmd

        def __log(self, msg, context=None):
                """Used to notify callers about operations performed by the
                catalog."""
                if self.verbose and "cherrypy" in globals():
                        # XXX generic logging mechanism needed
                        cherrypy.log(msg, context)

        def __index_log(self, msg):
                self.__log(msg, "INDEX")

        def __set_perms(self):
                """Sets permissions on catalog files if not read_only and if the
                current user can do so; raises CatalogPermissionsException if
                the permissions are wrong and cannot be corrected."""

                apath = os.path.normpath(os.path.join(self.catalog_root,
                    "attrs"))
                cpath = os.path.normpath(os.path.join(self.catalog_root,
                    "catalog"))

                # Force file_mode, so that unprivileged users can read these.
                bad_modes = []
                for fpath in (apath, cpath):
                        try:
                                if self.read_only:
                                        try:
                                                portable.assert_mode(fpath,
                                                    self.file_mode)
                                        except AssertionError, ae:
                                                bad_modes.append((fpath,
                                                    "%o" % self.file_mode,
                                                    "%o" % ae.mode))
                                else:
                                        os.chmod(fpath, self.file_mode)
                        except EnvironmentError, e:
                                # If the files don't exist yet, move on.
                                if e.errno == errno.ENOENT:
                                        continue

                                # If the mode change failed for another reason,
                                # check to see if we actually needed to change
                                # it, and if so, add it to bad_modes.
                                try:
                                        portable.assert_mode(fpath,
                                            self.file_mode)
                                except AssertionError, ae:
                                        bad_modes.append((fpath,
                                            "%o" % self.file_mode,
                                            "%o" % ae.mode))
 
                if bad_modes:
                        raise CatalogPermissionsException(bad_modes)

        def add_fmri(self, pfmri, critical = False):
                """Add a package, named by the fmri, to the catalog.
                Throws an exception if an identical package is already
                present.  Throws an exception if package has no version."""
                if pfmri.version == None:
                        raise CatalogException, \
                            "Unversioned FMRI not supported: %s" % pfmri

                assert not self.read_only

                # Callers should verify that the FMRI they're going to add is
                # valid; however, this check is here in case they're
                # lackadaisical
                if not self.valid_new_fmri(pfmri):
                        raise CatalogException("FMRI %s already exists in "
                            "the catalog." % pfmri)

                if critical:
                        pkgstr = "C %s\n" % pfmri.get_fmri(anarchy = True)
                else:
                        pkgstr = "V %s\n" % pfmri.get_fmri(anarchy = True)


                self.catalog_lock.acquire()
                try:
                        self.__append_to_catalog(pkgstr)

                        # Catalog size has changed, force recalculation on
                        # next send()
                        self.__size = -1

                        self.attrs["npkgs"] += 1

                        ts = datetime.datetime.now()
                        self.set_time(ts)
                finally:
                        self.catalog_lock.release()

                return ts

        def __append_to_catalog(self, pkgstr):
                """Write string named pkgstr to the catalog.  This
                routine handles moving the catalog to a temporary file,
                appending the new string, and renaming the temporary file
                on top of the existing catalog."""

                # Create tempfile
                tmp_num, tmpfile = tempfile.mkstemp(dir=self.catalog_root)

                try:
                        # use fdopen since we already have a filehandle
                        tfile = os.fdopen(tmp_num, "w")
                except OSError:
                        portable.remove(tmpfile)
                        raise

                # Try to open catalog file.  If it doesn't exist,
                # create an empty catalog file, and then open it read only.
                try:
                        pfile = file(self.catalog_file, "rb")
                except IOError, e:
                        if e.errno == errno.ENOENT:
                                # Creating an empty file
                                file(self.catalog_file, "wb").close()
                                pfile = file(self.catalog_file, "rb")
                        else:
                                portable.remove(tmpfile)
                                raise

                # Make sure we're at the start of the file
                pfile.seek(0)

                # Write all of the existing entries in the catalog
                # into the tempfile.  Then append the new lines at the
                # end.
                try:
                        for entry in pfile:
                                if entry == pkgstr:
                                        raise CatalogException(
                                            "Package %s is already in " 
                                            "the catalog" % pkgstr)
                                else:
                                        tfile.write(entry)
                        tfile.write(pkgstr)
                except Exception:
                        portable.remove(tmpfile)
                        raise

                # Close our open files
                pfile.close()
                tfile.close()

                # Set the permissions on the tempfile correctly.
                # Mkstemp creates files as 600.  Rename the new
                # cataog on top of the old one.
                try:
                        os.chmod(tmpfile, self.file_mode)
                        portable.rename(tmpfile, self.catalog_file)
                except EnvironmentError:
                        portable.remove(tmpfile)
                        raise

        @staticmethod
        def fast_cache_fmri(d, pfmri, sversion, pubs):
                """Store the fmri in a data structure 'd' for fast lookup, but
                requires the caller to provide all the data pre-sorted and
                processed.

                'd' is a dict that maps each package name to another dictionary

                'pfmri' is the fmri object to be cached.

                'sversion' is the string representation of pfmri.version.

                'pubs' is a dict of publisher name and boolean value pairs
                indicating catalog presence.

                The fmri is expected not to have an embedded publisher.  If it
                does, it will be ignored.

                See cache_fmri() for data structure details."""

                if pfmri.pkg_name not in d:
                        # This is the simplest representation of the cache data
                        # structure.
                        d[pfmri.pkg_name] = {
                            "versions": [pfmri.version],
                            sversion: (pfmri, pubs)
                        }
                else:
                        # It's assumed the caller will provide these in
                        # the correct order for performance reasons.
                        d[pfmri.pkg_name][sversion] = (pfmri, pubs)
                        d[pfmri.pkg_name]["versions"].append(pfmri.version)

        @staticmethod
        def cache_fmri(d, pfmri, pub, known=True):
                """Store the fmri in a data structure 'd' for fast lookup.

                'd' is a dict that maps each package name to another dictionary,
                itself mapping:
                
                        * each version string, which maps to a tuple of:
                          -- the fmri object
                          -- a dict of publisher prefixes with each value
                             indicating catalog presence

                        * "versions", which maps to a list of version objects,
                          kept in sorted order

                The structure is as follows:
                    pkg_name1: {
                        "versions": [<version1>, <version2>, ... ],
                        "version1": (
                            <fmri1>,
                            { "pub1": known, "pub2": known, ... },
                        ),
                        "version2": (
                            <fmri2>,
                            { "pub1": known, "pub2": known, ... },
                        ),
                        ...
                    },
                    pkg_name2: {
                        ...
                    },
                    ...

                (where names in quotes are strings, names in angle brackets are
                objects, and the rest of the syntax is Pythonic).

                The fmri is expected not to have an embedded publisher.  If it
                does, it will be ignored."""

                if pfmri.has_publisher():
                        # Cache entries must not contain the name of the
                        # publisher, otherwise matching during packaging
                        # operations may not work correctly.
                        pfmri = fmri.PkgFmri(pfmri.get_fmri(anarchy=True))

                pversion = str(pfmri.version)
                if pfmri.pkg_name not in d:
                        # This is the simplest representation of the cache data
                        # structure.
                        d[pfmri.pkg_name] = {
                            "versions": [pfmri.version],
                            pversion: (pfmri, { pub: known })
                        }

                elif pversion not in d[pfmri.pkg_name]:
                        d[pfmri.pkg_name][pversion] = (pfmri, { pub: known })
                        bisect.insort(d[pfmri.pkg_name]["versions"],
                            pfmri.version)
                elif pub not in d[pfmri.pkg_name][pversion][1]:
                        d[pfmri.pkg_name][pversion][1][pub] = known

        def added_prefix(self, p):
                """Perform any catalog transformations necessary if
                prefix p is found in the catalog.  Previously, we didn't
                know how to handle this prefix and now we do.  If we
                need to transform the entry from server to client form,
                make sure that happens here."""

                # Nothing to do now.
                pass

        def attrs_as_lines(self):
                """Takes the list of in-memory attributes and returns
                a list of strings, each string naming an attribute."""

                ret = []

                for k, v in self.attrs.items():
                        s = "S %s: %s\n" % (k, v)
                        ret.append(s)

                return ret

        def as_lines(self):
                """Returns a generator function that produces the contents of
                the catalog as a list of strings."""

                try:
                        cfile = file(self.catalog_file, "r")
                except EnvironmentError, e:
                        # Missing catalog is fine; other errors need to
                        # be reported.
                        if e.errno == errno.ENOENT:
                                return
                        raise

                for e in cfile:
                        yield e

                cfile.close()

        @staticmethod
        def _fmri_from_path(pkg, vers):
                """Helper method that takes the full path to the package
                directory and the name of the manifest file, and returns an FMRI
                constructed from the information in those components."""

                v = version.Version(urllib.unquote(vers), None)
                f = fmri.PkgFmri(urllib.unquote(os.path.basename(pkg)), None)
                f.version = v
                return f

        def check_prefix(self):
                """If this version of the catalog knows about new prefixes,
                check the on disk catalog to see if we can perform any
                transformations based upon previously unknown catalog formats.

                This routine will add a catalog attribute if it doesn't exist,
                otherwise it checks this attribute against a hard-coded
                version-specific tuple to see if new methods were added.

                If new methods were added, it will call an additional routine
                that updates the on-disk catalog, if necessary."""


                # If a prefixes attribute doesn't exist, write one and get on
                # with it.
                if not "prefix" in self.attrs:
                        self.attrs["prefix"] = "".join(known_prefixes)
                        if not self.read_only:
                                self.save_attrs()
                        return

                # Prefixes attribute does exist.  Check if it has changed.
                pfx_set = set(self.attrs["prefix"])

                # Nothing to do if prefixes haven't changed
                if pfx_set == known_prefixes:
                        return

                # If known_prefixes contains a prefix not in pfx_set,
                # add the prefix and perform a catalog transform.
                new = known_prefixes.difference(pfx_set)
                if new:
                        for p in new:
                                self.added_prefix(p)

                        pfx_set.update(new)

                        # Write out updated prefixes list
                        self.attrs["prefix"] = "".join(pfx_set)
                        if not self.read_only:
                                self.save_attrs()

        @property
        def exists(self):
                """A boolean value indicating whether the Catalog exists
                on-disk."""

                if not self.catalog_file:
                        return False
                return os.path.exists(self.catalog_file)

        # XXX This is only used by a handful of tests.
        def get_matching_fmris(self, patterns):
                """Wrapper for extract_matching_fmris."""

                if self.attrs["npkgs"] == 0:
                        return []

                ret = extract_matching_fmris(self.fmris(), patterns)

                return sorted(ret, reverse = True)

        def fmris(self):
                """A generator function that produces FMRIs as it
                iterates over the contents of the catalog."""

                try:
                        pfile = file(os.path.normpath(
                            os.path.join(self.catalog_root, "catalog")), "r")
                except IOError, e:
                        if e.errno == errno.ENOENT:
                                return
                        else:
                                raise

                for entry in pfile:
                        if not entry.startswith("V pkg") and \
                            not entry.startswith("C pkg"):
                                continue

                        try:
                                yield self.__parse_entry(entry, self.pub)
                        except (KeyboardInterrupt, SystemExit):
                                raise
                        except Exception, e:
                                raise RuntimeError("corrupt catalog entry for "
                                    "publisher '%s': %s" % (self.pub, entry))

                pfile.close()

        def last_modified(self):
                """Return the time at which the catalog was last modified."""

                return self.attrs.get("Last-Modified", None)

        def load_attrs(self, filenm = "attrs"):
                """Load attributes from the catalog file into the in-memory
                attributes dictionary"""

                apath = os.path.normpath(
                    os.path.join(self.catalog_root, filenm))
                if not os.path.exists(apath):
                        return

                afile = file(apath, "r")
                attrre = re.compile('^S ([^:]*): (.*)')

                for entry in afile:
                        m = attrre.match(entry)
                        if m != None:
                                self.attrs[m.group(1)] = m.group(2)

                afile.close()

                # convert npkgs to integer value
                if "npkgs" in self.attrs:
                        self.attrs["npkgs"] = int(self.attrs["npkgs"])

        def npkgs(self):
                """Returns the number of packages in the catalog."""

                return self.attrs["npkgs"]

        def origin(self):
                """Returns the URL of the catalog's origin."""

                return self.attrs.get("origin", None)

        @classmethod
        def recv(cls, filep, path, pub=None):
                """A static method that takes a file-like object and
                a path.  This is the other half of catalog.send().  It
                reads a stream as an incoming catalog and lays it down
                on disk."""

                bad_fmri = None

                if not os.path.exists(path):
                        os.makedirs(path)

                afd, attrpath = tempfile.mkstemp(dir=path)
                cfd, catpath = tempfile.mkstemp(dir=path)

                attrf = os.fdopen(afd, "w")
                catf = os.fdopen(cfd, "w")

                attrpath_final = os.path.normpath(os.path.join(path, "attrs"))
                catpath_final = os.path.normpath(os.path.join(path, "catalog"))

                try:
                        for s in filep:
                                slen = len(s)

                                # If line is too short, process the next one
                                if slen < 2:
                                        continue
                                # check that line is in the proper format
                                elif not s[1].isspace():
                                        continue
                                elif not s[0] in known_prefixes:
                                        catf.write(s)
                                elif s.startswith("S "):
                                        attrf.write(s)
                                elif s.startswith("R "):
                                        catf.write(s)
                                else:
                                        # XXX Need to be able to handle old and
                                        # new format catalogs.
                                        try:
                                                f = fmri.PkgFmri(s[2:])
                                        except fmri.IllegalFmri, e:
                                                bad_fmri = e
                                                continue

                                        catf.write("%s %s %s %s\n" %
                                            (s[0], "pkg", f.pkg_name,
                                            f.version))
                except:
                        # Re-raise all uncaught exceptions after performing
                        # cleanup.
                        attrf.close()
                        catf.close()
                        os.remove(attrpath)
                        os.remove(catpath)
                        raise

                # If we got a parse error on FMRIs and transfer
                # wasn't truncated, raise a FmriFailures error.
                if bad_fmri:
                        attrf.close()
                        catf.close()
                        os.remove(attrpath)
                        os.remove(catpath)
                        raise bad_fmri

                # Write the publisher's origin into our attributes
                if pub:
                        origstr = "S origin: %s\n" % pub["origin"]
                        attrf.write(origstr)

                attrf.close()
                catf.close()

                # Mkstemp sets mode 600 on these files by default.
                # Restore them to 644, so that unprivileged users
                # may read these files.
                os.chmod(attrpath, cls.file_mode)
                os.chmod(catpath, cls.file_mode)

                portable.rename(attrpath, attrpath_final)
                portable.rename(catpath, catpath_final)

        def save_attrs(self, filenm="attrs"):
                """Save attributes from the in-memory catalog to a file
                specified by filenm."""

                tmpfile = None
                assert not self.read_only

                finalpath = os.path.normpath(
                    os.path.join(self.catalog_root, filenm))

                try:
                        tmp_num, tmpfile = tempfile.mkstemp(
                            dir=self.catalog_root)

                        tfile = os.fdopen(tmp_num, "w")

                        for a in self.attrs.keys():
                                s = "S %s: %s\n" % (a, self.attrs[a])
                                tfile.write(s)

                        tfile.close()
                        os.chmod(tmpfile, self.file_mode)
                        portable.rename(tmpfile, finalpath)

                except EnvironmentError, e:
                        # This may get called in a situation where
                        # the user does not have write access to the attrs
                        # file.
                        if tmpfile:
                                portable.remove(tmpfile)
                        if e.errno == errno.EACCES:
                                return
                        else:
                                raise

                # Recalculate size on next send()
                self.__size = -1

        def send(self, filep, rspobj=None):
                """Send the contents of this catalog out to the filep
                specified as an argument."""

                if rspobj is not None:
                        rspobj.headers['Content-Length'] = str(self.size())

                def output():
                        # Send attributes first.
                        for line in self.attrs_as_lines():
                                yield line

                        try:
                                cfile = file(os.path.normpath(
                                    os.path.join(self.catalog_root, "catalog")),
                                    "r")
                        except IOError, e:
                                # Missing catalog is fine; other errors need to
                                # be reported.
                                if e.errno == errno.ENOENT:
                                        return
                                else:
                                        raise

                        for e in cfile:
                                yield e

                        cfile.close()

                if filep:
                        for line in output():
                                filep.write(line)
                else:
                        return output()

        def set_time(self, ts = None):
                """Set time to timestamp if supplied by caller.  Otherwise
                use the system time."""

                assert not self.read_only

                if ts and isinstance(ts, str):
                        self.attrs["Last-Modified"] = ts
                elif ts and isinstance(ts, datetime.datetime):
                        self.attrs["Last-Modified"] = ts.isoformat()
                else:
                        self.attrs["Last-Modified"] = timestamp()

                self.save_attrs()

        def size(self):
                """Return the size in bytes of the catalog and attributes."""

                if self.__size < 0:
                        try:
                                attr_stat = os.stat(os.path.normpath(
                                    os.path.join(self.catalog_root, "attrs")))
                                attr_sz = attr_stat.st_size
                        except OSError, e:
                                if e.errno == errno.ENOENT:
                                        attr_sz = 0
                                else:
                                        raise
                        try:
                                cat_stat =  os.stat(os.path.normpath(
                                    os.path.join(self.catalog_root, "catalog")))
                                cat_sz = cat_stat.st_size
                        except OSError, e:
                                if e.errno == errno.ENOENT:
                                        cat_sz = 0
                                else:
                                        raise

                        self.__size = attr_sz + cat_sz

                return self.__size

        def valid_new_fmri(self, pfmri):
                """Check that the fmri supplied as an argument would be valid
                to add to the catalog.  This checks to make sure that any past
                catalog operations (such as a rename or freeze) would not
                prohibit the caller from adding this FMRI."""

                if not fmri.is_valid_pkg_name(pfmri.get_name()):
                        return False
                return True

        def refresh_index(self):
                """ This function refreshes the search indexes if there any new
                packages. It starts a subprocess which results in a call to
                run_update_index (see below) which does the actual update.
                """

                self.searchdb_update_handle_lock.acquire()

                if self.searchdb_update_handle:
                        self.refresh_again = True
                        self.searchdb_update_handle_lock.release()
                        return

                try:
                        fmris_to_index = set(self.fmris())

                        indexer.Indexer.check_for_updates(self.index_root,
                            fmris_to_index)

                        if fmris_to_index:
                                if os.name == "posix" and self.fork_allowed:
                                        cmd = self.whence(sys.argv[0])
                                        args = (sys.executable, cmd,
                                            "--refresh-index", "-d",
                                            self.repo_root)
                                        if os.path.normpath(
                                            self.index_root) != \
                                            os.path.normpath(os.path.join(
                                            self.repo_root, "index")):
                                                writ, t = os.path.split(
                                                    self.index_root)
                                                args += ("--writable-root",
                                                    writ)
                                        if self.read_only:
                                                args += ("--readonly",)
                                        try:
                                                self.searchdb_update_handle = \
                                                    subprocess.Popen(args,
                                                    stderr=subprocess.STDOUT)
                                        except Exception, e:
                                                self.__log("Starting the "
                                                    "indexing process failed: "
                                                    "%s" % e)
                                                raise
                                else:
                                        self.run_update_index()
                        else:
                                # Since there is nothing to index, setup
                                # the index and declare search available.
                                # We only log this if this represents
                                # a change in status of the server.
                                ind = indexer.Indexer(self.index_root,
                                    self.get_server_manifest,
                                    self.get_manifest_path,
                                    log=self.__index_log)
                                ind.setup()
                                if not self._search_available:
                                        self.__index_log("Search Available")
                                self._search_available = True
                finally:
                        self.searchdb_update_handle_lock.release()

        def run_update_index(self):
                """ Determines which fmris need to be indexed and passes them
                to the indexer.

                Note: Only one instance of this method should be running.
                External locking is expected to ensure this behavior. Calling
                refresh index is the preferred method to use to reindex.
                """
                fmris_to_index = set(self.fmris())

                indexer.Indexer.check_for_updates(self.index_root,
                    fmris_to_index)

                if fmris_to_index:
                        self.__index_log("Updating search indices")
                        self.__update_searchdb_unlocked(fmris_to_index)
                else:
                        ind = indexer.Indexer(self.index_root,
                            self.get_server_manifest, self.get_manifest_path,
                            log=self.__index_log)
                        ind.setup()

        def _check_search(self):
                if not self.index_root:
                        return

                ind = indexer.Indexer(self.index_root,
                    self.get_server_manifest, self.get_manifest_path,
                    log=self.__index_log)
                cie = False
                try:
                        cie = ind.check_index_existence()
                except se.InconsistentIndexException:
                        pass
                if cie:
                        self._search_available = True
                        self.__index_log("Search Available")

        def build_catalog(self):
                """ Creates an Indexer instance and after building the
                catalog, refreshes the index.
                """
                self._check_search()

                try:
                        cat_mtime = os.stat(os.path.join(
                            self.catalog_root, "catalog")).st_mtime
                except OSError, e:
                        if e.errno != errno.ENOENT:
                                raise
                        cat_mtime = 0

                # XXX eschew os.walk in favor of another os.listdir here?
                tree = os.walk(self.pkg_root)
                for pkg in tree:
                        if pkg[0] == self.pkg_root:
                                continue

                        for e in os.listdir(pkg[0]):
                                ver_mtime = os.stat(os.path.join(
                                    pkg[0], e)).st_mtime

                                # XXX force a rebuild despite mtimes?
                                # XXX queue this and fork later?
                                if ver_mtime > cat_mtime:
                                        f = self._fmri_from_path(pkg[0], e)
                                        self.add_fmri(f)
                                        print f

                # refresh_index doesn't use file modification times
                # to determine which packages need to be indexed, so use
                # it to reindex if it's needed.
                self.refresh_index()

        def child_handler(self, sig, frame):
                """ Handler method for the SIGCHLD signal.  Checks to see if the
                search database update child has finished, and enables searching
                if it finished successfully, or logs an error if it didn't.
                """
                try:
                        signal.signal(signal.SIGCHLD, self.child_handler)
                except ValueError:
                        self.__log("Tried to create signal handler in a thread "
                            "other than the main thread.")
                # If there's no update_handle, then another subprocess was
                # spun off and that was what finished. If the poll() returns
                # None, then while the indexer was running, another process
                # that was spun off finished.
                rc = None
                if not self.searchdb_update_handle:
                        return
                rc = self.searchdb_update_handle.poll()
                if rc == None:
                        return

                if rc == 0:
                        self._search_available = True
                        self.__index_log(
                            "Search indexes updated and available.")
                        # Need to acquire this lock to prevent the possibility
                        # of a race condition with refresh_index where a needed
                        # refresh is dropped. It is possible that an extra
                        # refresh will be done with this code, but that refresh
                        # should be very quick to finish.
                        self.searchdb_update_handle_lock.acquire()
                        self.searchdb_update_handle = None
                        self.searchdb_update_handle_lock.release()

                        if self.refresh_again:
                                self.refresh_again = False
                                self.refresh_index()
                elif rc > 0:
                        # If the refresh of the index failed, defensively
                        # declare that search is unavailable.
                        self.__index_log("ERROR building search database, exit "
                            "code: %s" % rc)
                        try:
                                self.__log(
                                    self.searchdb_update_handle.stderr.read())
                                self.searchdb_update_handle.stderr.read()
                        except KeyboardInterrupt:
                                raise
                        except:
                                pass
                        self.searchdb_update_handle_lock.acquire()
                        self.searchdb_update_handle = None
                        self.searchdb_update_handle_lock.release()

        def __update_searchdb_unlocked(self, fmris):
                """ Creates an indexer then hands it fmris It assumes that all
                needed locking has already occurred.
                """
                assert self.index_root

                if fmris:
                        index_inst = indexer.Indexer(self.index_root,
                            self.get_server_manifest, self.get_manifest_path,
                            log=self.__index_log)
                        index_inst.server_update_index(fmris)

        def get_manifest_path(self, f):
                return os.path.join(self.pkg_root, f.get_dir_path())

        def get_server_manifest(self, f, add_to_cache=False):
                assert not add_to_cache
                m = manifest.Manifest()
                mcontent = file(self.get_manifest_path(f)).read()
                m.set_fmri(None, fmri)
                m.set_content(mcontent, EmptyI)
                return m

        def search(self, q):
                """Searches the index using the information given by "q", a
                Query object."""

                assert self.index_root
                l = query_p.QueryLexer()
                l.build()
                qp = query_p.QueryParser(l)
                query = qp.parse(q.encoded_text())
                query.set_info(num_to_return=q.num_to_return,
                    start_point=q.start_point, index_dir=self.index_root,
                    get_manifest_path=self.get_manifest_path,
                    case_sensitive=q.case_sensitive)
                return query.search(self.fmris)                

        def search_available(self):
                return self._search_available or self._check_search()

        @staticmethod
        def __parse_entry(line, pub):
                # This allows the ServerCatalog object to parse both
                # client and server catalog files which are otherwise
                # identical.
                #
                # Server Format:
                # C pkg:/foo@0.5.11,5.11-0.111:20090507T161015Z
                #
                # Client Format:
                # V pkg foo 0.5.11,5.11-0.111:20090508T161015Z
                sfmri = line[2:]
                if sfmri[:4] == "pkg ":
                        sfmri = sfmri[4:].replace(" ", "@")
                return fmri.PkgFmri(sfmri, publisher=pub)

        @classmethod
        def read_catalog(cls, cat, path, pub=None):
                """Read the catalog file in "path" and combine it with the
                existing data in "catalog"."""

                catf = file(os.path.join(path, "catalog"))
                for line in catf:
                        if not line.startswith("V pkg") and \
                            not line.startswith("C pkg"):
                                continue
                        f = cls.__parse_entry(line, pub)
                        ServerCatalog.cache_fmri(cat, f, pub)

                catf.close()

class NastyServerCatalog(ServerCatalog):
        """The catalog for the nasty server."""

        def as_lines(self, scfg=None):
                """Returns a generator function that produces the contents of
                the catalog as a list of strings."""

                be_nasty = False

                # NASTY
                # First roll the dice to decide whether we should be nasty.
                # Later roll again to decide when to be nasty.
                if scfg and scfg.need_nasty_occasionally():
                        be_nasty = True

                try:
                        cfile = file(self.catalog_file, "r")
                except EnvironmentError, e:
                        # Missing catalog is fine; other errors need to
                        # be reported.
                        if e.errno == errno.ENOENT:
                                return
                        raise

                for e in cfile:
                        # NASTY
                        # There's only one opportunity to truncate
                        # the request, but if we don't truncate the
                        # request we can try to truncate a line too.
                        if be_nasty and scfg.need_nasty_occasionally():
                                return
                        elif be_nasty and \
                            scfg.need_nasty_infrequently(): 
                                linelen = random.randint(1, len(e))
                                badline = e[0:linelen]
                                yield badline
                        else:
                                yield e

                cfile.close()

# Prefixes that this catalog knows how to handle
known_prefixes = frozenset("CSVR")

# Method used by Catalog and UpdateLog.  Since UpdateLog needs to know
# about Catalog, keep it in Catalog to avoid circular dependency problems.
def timestamp():
        """Return an integer timestamp that can be used for comparisons."""

        tobj = datetime.datetime.now()
        tstr = tobj.isoformat()
        return tstr

def ts_to_datetime(ts):
        """Take timestamp ts in string isoformat, and convert it to a datetime
        object."""

        year = int(ts[0:4])
        month = int(ts[5:7])
        day = int(ts[8:10])
        hour = int(ts[11:13])
        minutes = int(ts[14:16])
        sec = int(ts[17:19])
        # usec is not in the string if 0
        try:
                usec = int(ts[20:26])
        except ValueError:
                usec = 0
        return datetime.datetime(year, month, day, hour, minutes, sec, usec)

def extract_matching_fmris(pkgs, patterns=None, matcher=None,
    constraint=None, counthash=None, versions=None):
        """Iterate through the given list of PkgFmri objects,
        looking for packages matching 'pattern' in 'patterns', based on the
        function in 'matcher' and the versioning constraint described by
        'constraint'.  If 'matcher' is None, uses fmri subset matching
        as the default.  If 'patterns' is None, 'versions' may be specified,
        and looks for packages matching the patterns specified in 'versions'.
        When using 'version', the 'constraint' parameter is ignored.

        'versions' should be a list of strings of the format:
            release,build_release-branch:datetime 

        ...with a value of '*' provided for any component to be ignored. '*' or
        '?' may be used within each component value and will act as wildcard
        characters ('*' for one or more characters, '?' for a single character).

        Returns a sorted list of PkgFmri objects, newest versions first.  If
        'counthash' is a dictionary, instead store the number of matched fmris
        for each package that matches."""

        if not matcher:
                matcher = fmri.fmri_match

        if patterns is None:
                patterns = []
        elif not isinstance(patterns, list):
                patterns = [ patterns ]

        if versions is None:
                versions = []
        elif not isinstance(versions, list):
                versions = [ version.MatchingVersion(versions, None) ]
        else:
                for i, ver in enumerate(versions):
                        versions[i] = version.MatchingVersion(ver, None)

        # 'pattern' may be a partially or fully decorated fmri; we want
        # to extract its name and version to match separately against
        # the catalog.
        # XXX "5.11" here needs to be saner
        tuples = {}

        for pattern in patterns:
                if isinstance(pattern, fmri.PkgFmri):
                        tuples[pattern] = pattern.tuple()
                else:
                        assert pattern != None
                        tuples[pattern] = \
                            fmri.PkgFmri(pattern, "5.11").tuple()

        def by_pattern(p):
                cat_pub, cat_name, cat_version = p.tuple()
                for pattern in patterns:
                        pat_pub, pat_name, pat_version = tuples[pattern]
                        if (fmri.is_same_publisher(pat_pub, cat_pub) or not \
                            pat_pub) and matcher(cat_name, pat_name):
                                if not pat_version or \
                                    p.version.is_successor(
                                    pat_version, constraint) or \
                                    p.version == pat_version:
                                        if counthash is not None:
                                                if pattern in counthash:
                                                        counthash[pattern] += 1
                                                else:
                                                        counthash[pattern] = 1

                                        if pat_pub:
                                                p.set_publisher(pat_pub)
                                        return p

        def by_version(p):
                for ver in versions:
                        if ver == p.version:
                                if counthash is not None:
                                        sver = str(ver)
                                        if sver in counthash:
                                                counthash[sver] += 1
                                        else:
                                                counthash[sver] = 1
                                return p

        ret = []
        if patterns:
                for p in pkgs:
                        res = by_pattern(p)
                        if res is not None:
                                ret.append(res)
        elif versions:
                for p in pkgs:
                        res = by_version(p)
                        if res is not None:
                                ret.append(res)

        return sorted(ret, reverse=True)

