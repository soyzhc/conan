import os

from conans.client.graph.graph import (BINARY_BUILD, BINARY_CACHE, BINARY_DOWNLOAD, BINARY_MISSING,
                                       BINARY_SKIP, BINARY_UPDATE, BINARY_WORKSPACE,
                                       RECIPE_EDITABLE, BINARY_EDITABLE,
                                       RECIPE_CONSUMER, RECIPE_VIRTUAL)
from conans.errors import NoRemoteAvailable, NotFoundException
from conans.model.info import ConanInfo
from conans.model.manifest import FileTreeManifest
from conans.model.ref import PackageReference
from conans.util.files import is_dirty, rmdir


class GraphBinariesAnalyzer(object):
    def __init__(self, cache, output, remote_manager, workspace):
        self._cache = cache
        self._out = output
        self._remote_manager = remote_manager
        self._registry = cache.registry
        self._workspace = workspace

    def _check_update(self, upstream_manifest, package_folder, output, node):
        read_manifest = FileTreeManifest.load(package_folder)
        if upstream_manifest != read_manifest:
            if upstream_manifest.time > read_manifest.time:
                output.warn("Current package is older than remote upstream one")
                node.update_manifest = upstream_manifest
                return True
            else:
                output.warn("Current package is newer than remote upstream one")

    def _evaluate_node(self, node, build_mode, update, evaluated_nodes, remote_name):
        assert node.binary is None, "Node binary is None"

        ref, conanfile = node.ref, node.conanfile
        package_id = conanfile.info.package_id()
        pref = PackageReference(ref, package_id)
        node.pref = pref
        # Check that this same reference hasn't already been checked
        previous_node = evaluated_nodes.get(pref)
        if previous_node:
            node.binary = previous_node.binary
            node.binary_remote = previous_node.binary_remote
            return
        evaluated_nodes[pref] = node

        output = conanfile.output

        if node.recipe == RECIPE_EDITABLE:
            node.binary = BINARY_EDITABLE
            return

        if build_mode.forced(conanfile, ref):
            output.warn('Forced build from source')
            node.binary = BINARY_BUILD
            return

        package_folder = self._cache.package(pref, short_paths=conanfile.short_paths)

        # Check if dirty, to remove it
        local_project = self._workspace[ref] if self._workspace else None
        if local_project:
            node.binary = BINARY_WORKSPACE
            return

        with self._cache.package_lock(pref):
            assert node.recipe != RECIPE_EDITABLE, "Editable package shouldn't reach this code"
            if is_dirty(package_folder):
                output.warn("Package is corrupted, removing folder: %s" % package_folder)
                rmdir(package_folder)  # Do not remove if it is EDITABLE

            if self._cache.config.revisions_enabled:
                metadata = self._cache.package_layout(pref.ref).load_metadata()
                rec_rev = metadata.packages[pref.id].recipe_revision
                if rec_rev and rec_rev != node.ref.revision:
                    output.warn("The package {} doesn't belong "
                                "to the installed recipe revision, removing folder".format(pref))
                    rmdir(package_folder)

        if remote_name:
            remote = self._registry.remotes.get(remote_name)
        else:
            # If the remote_name is not given, follow the binary remote, or
            # the recipe remote
            # If it is defined it won't iterate (might change in conan2.0)
            remote = self._registry.prefs.get(pref) or self._registry.refs.get(ref)
        remotes = self._registry.remotes.list

        if os.path.exists(package_folder):
            if update:
                if remote:
                    try:
                        tmp = self._remote_manager.get_package_manifest(pref, remote)
                        upstream_manifest, pref = tmp
                    except NotFoundException:
                        output.warn("Can't update, no package in remote")
                    except NoRemoteAvailable:
                        output.warn("Can't update, no remote defined")
                    else:
                        if self._check_update(upstream_manifest, package_folder, output, node):
                            node.binary = BINARY_UPDATE
                            node.pref = pref  # With revision
                            if build_mode.outdated:
                                info, pref = self._remote_manager.get_package_info(pref, remote)
                                package_hash = info.recipe_hash()
                elif remotes:
                    pass
                else:
                    output.warn("Can't update, no remote defined")
            if not node.binary:
                node.binary = BINARY_CACHE
                package_hash = ConanInfo.load_from_package(package_folder).recipe_hash

        else:  # Binary does NOT exist locally
            remote_info = None
            if remote:
                try:
                    remote_info, pref = self._remote_manager.get_package_info(pref, remote)
                except NotFoundException:
                    pass

            # If the "remote" came from the registry but the user didn't specified the -r, with
            # revisions iterate all remotes
            if not remote or (not remote_info and self._cache.config.revisions_enabled
                              and not remote_name):
                for r in remotes:
                    try:
                        remote_info, pref = self._remote_manager.get_package_info(pref, r)
                    except NotFoundException:
                        pass
                    else:
                        if remote_info:
                            remote = r
                            break

            if remote_info:
                node.binary = BINARY_DOWNLOAD
                node.pref = pref  # With PREF
                package_hash = remote_info.recipe_hash
            else:
                if build_mode.allowed(conanfile):
                    node.binary = BINARY_BUILD
                else:
                    node.binary = BINARY_MISSING

        if build_mode.outdated:
            if node.binary in (BINARY_CACHE, BINARY_DOWNLOAD, BINARY_UPDATE):
                local_recipe_hash = self._cache.package_layout(ref).recipe_manifest().summary_hash
                if local_recipe_hash != package_hash:
                    output.info("Outdated package!")
                    node.binary = BINARY_BUILD
                else:
                    output.info("Package is up to date")

        node.binary_remote = remote

    def evaluate_graph(self, deps_graph, build_mode, update, remote_name):
        evaluated_nodes = {}
        for node in deps_graph.nodes:
            if node.recipe in (RECIPE_CONSUMER, RECIPE_VIRTUAL) or node.binary:
                continue
            private_neighbours = node.private_neighbors()
            if private_neighbours:
                self._evaluate_node(node, build_mode, update, evaluated_nodes, remote_name)
                if node.binary in (BINARY_CACHE, BINARY_DOWNLOAD, BINARY_UPDATE):
                    for neigh in private_neighbours:
                        neigh.binary = BINARY_SKIP
                        closure = deps_graph.full_closure(neigh, private=True)
                        for n in closure:
                            n.binary = BINARY_SKIP

        for node in deps_graph.nodes:
            if node.recipe in (RECIPE_CONSUMER, RECIPE_VIRTUAL) or node.binary:
                continue
            self._evaluate_node(node, build_mode, update, evaluated_nodes, remote_name)
