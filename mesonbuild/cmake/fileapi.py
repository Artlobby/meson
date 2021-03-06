# Copyright 2019 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from .common import CMakeException, CMakeBuildFile, CMakeConfiguration
from typing import Any, List, Tuple
from .. import mlog
import os
import json
import re

STRIP_KEYS = ['cmake', 'reply', 'backtrace', 'backtraceGraph', 'version']

class CMakeFileAPI:
    def __init__(self, build_dir: str):
        self.build_dir = build_dir
        self.api_base_dir = os.path.join(self.build_dir, '.cmake', 'api', 'v1')
        self.request_dir = os.path.join(self.api_base_dir, 'query', 'client-meson')
        self.reply_dir = os.path.join(self.api_base_dir, 'reply')
        self.cmake_sources = []
        self.cmake_configurations = []
        self.kind_resolver_map = {
            'codemodel': self._parse_codemodel,
            'cmakeFiles': self._parse_cmakeFiles,
        }

    def get_cmake_sources(self) -> List[CMakeBuildFile]:
        return self.cmake_sources

    def get_cmake_configurations(self) -> List[CMakeConfiguration]:
        return self.cmake_configurations

    def setup_request(self) -> None:
        os.makedirs(self.request_dir, exist_ok=True)

        query = {
            'requests': [
                {'kind': 'codemodel', 'version': {'major': 2, 'minor': 0}},
                {'kind': 'cmakeFiles', 'version': {'major': 1, 'minor': 0}},
            ]
        }

        with open(os.path.join(self.request_dir, 'query.json'), 'w') as fp:
            json.dump(query, fp, indent=2)

    def load_reply(self) -> None:
        if not os.path.isdir(self.reply_dir):
            raise CMakeException('No response from the CMake file API')

        files = os.listdir(self.reply_dir)
        root = None
        reg_index = re.compile(r'^index-.*\.json$')
        for i in files:
            if reg_index.match(i):
                root = i
                break

        if not root:
            raise CMakeException('Failed to find the CMake file API index')

        index = self._reply_file_content(root)   # Load the root index
        index = self._strip_data(index)          # Avoid loading duplicate files
        index = self._resolve_references(index)  # Load everything
        index = self._strip_data(index)          # Strip unused data (again for loaded files)

        # Debug output
        debug_json = os.path.normpath(os.path.join(self.build_dir, '..', 'fileAPI.json'))
        with open(debug_json, 'w') as fp:
            json.dump(index, fp, indent=2)
        mlog.cmd_ci_include(debug_json)

        # parse the JSON
        for i in index['objects']:
            assert(isinstance(i, dict))
            assert('kind' in i)
            assert(i['kind'] in self.kind_resolver_map)

            self.kind_resolver_map[i['kind']](i)

    def _parse_codemodel(self, data: dict) -> None:
        assert('configurations' in data)
        assert('paths' in data)

        source_dir = data['paths']['source']
        build_dir = data['paths']['build']

        # The file API output differs quite a bit from the server
        # output. It is more flat than the server output and makes
        # heavy use of references. Here these references are
        # resolved and the resulting data structure is identical
        # to the CMake serve output.

        def helper_parse_dir(dir_entry: dict) -> Tuple[str, str]:
            src_dir = dir_entry.get('source', '.')
            bld_dir = dir_entry.get('build', '.')
            src_dir = src_dir if os.path.isabs(src_dir) else os.path.join(source_dir, src_dir)
            bld_dir = bld_dir if os.path.isabs(bld_dir) else os.path.join(source_dir, bld_dir)
            src_dir = os.path.normpath(src_dir)
            bld_dir = os.path.normpath(bld_dir)

            return src_dir, bld_dir

        def parse_sources(comp_group: dict, tgt: dict) -> Tuple[List[str], List[str], List[int]]:
            gen = []
            src = []
            idx = []

            src_list_raw = tgt.get('sources', [])
            for i in comp_group.get('sourceIndexes', []):
                if i >= len(src_list_raw) or 'path' not in src_list_raw[i]:
                    continue
                if src_list_raw[i].get('isGenerated', False):
                    gen += [src_list_raw[i]['path']]
                else:
                    src += [src_list_raw[i]['path']]
                idx += [i]

            return src, gen, idx

        def parse_target(tgt: dict) -> dict:
            src_dir, bld_dir = helper_parse_dir(cnf.get('paths', {}))

            # Parse install paths (if present)
            install_paths = []
            if 'install' in tgt:
                prefix = tgt['install']['prefix']['path']
                install_paths = [os.path.join(prefix, x['path']) for x in tgt['install']['destinations']]
                install_paths = list(set(install_paths))

            # On the first look, it looks really nice that the CMake devs have
            # decided to use arrays for the linker flags. However, this feeling
            # soon turns into despair when you realize that there only one entry
            # per type in most cases, and we still have to do manual string splitting.
            link_flags = []
            link_libs = []
            for i in tgt.get('link', {}).get('commandFragments', []):
                if i['role'] == 'flags':
                    link_flags += [i['fragment']]
                elif i['role'] == 'libraries':
                    link_libs += [i['fragment']]
                elif i['role'] == 'libraryPath':
                    link_flags += ['-L{}'.format(i['fragment'])]
                elif i['role'] == 'frameworkPath':
                    link_flags += ['-F{}'.format(i['fragment'])]
            for i in tgt.get('archive', {}).get('commandFragments', []):
                if i['role'] == 'flags':
                    link_flags += [i['fragment']]

            # TODO The `dependencies` entry is new in the file API.
            #      maybe we can make use of that in addition to the
            #      implicit dependency detection
            tgt_data = {
                'artifacts': [x.get('path', '') for x in tgt.get('artifacts', [])],
                'sourceDirectory': src_dir,
                'buildDirectory': bld_dir,
                'name': tgt.get('name', ''),
                'fullName': tgt.get('nameOnDisk', ''),
                'hasInstallRule': 'install' in tgt,
                'installPaths': install_paths,
                'linkerLanguage': tgt.get('link', {}).get('language', 'CXX'),
                'linkLibraries': ' '.join(link_libs),  # See previous comment block why we join the array
                'linkFlags': ' '.join(link_flags),     # See previous comment block why we join the array
                'type': tgt.get('type', 'EXECUTABLE'),
                'fileGroups': [],
            }

            processed_src_idx = []
            for cg in tgt.get('compileGroups', []):
                # Again, why an array, when there is usually only one element
                # and arguments are separated with spaces...
                flags = []
                for i in cg.get('compileCommandFragments', []):
                    flags += [i['fragment']]

                cg_data = {
                    'defines': [x.get('define', '') for x in cg.get('defines', [])],
                    'compileFlags': ' '.join(flags),
                    'language': cg.get('language', 'C'),
                    'isGenerated': None,  # Set later, flag is stored per source file
                    'sources': [],
                    'includePath': cg.get('includes', []),
                }

                normal_src, generated_src, src_idx = parse_sources(cg, tgt)
                if normal_src:
                    cg_data = dict(cg_data)
                    cg_data['isGenerated'] = False
                    cg_data['sources'] = normal_src
                    tgt_data['fileGroups'] += [cg_data]
                if generated_src:
                    cg_data = dict(cg_data)
                    cg_data['isGenerated'] = True
                    cg_data['sources'] = generated_src
                    tgt_data['fileGroups'] += [cg_data]
                processed_src_idx += src_idx

            # Object libraries have no compile groups, only source groups.
            # So we add all the source files to a dummy source group that were
            # not found in the previous loop
            normal_src = []
            generated_src = []
            for idx, src in enumerate(tgt.get('sources', [])):
                if idx in processed_src_idx:
                    continue

                if src.get('isGenerated', False):
                    generated_src += [src['path']]
                else:
                    normal_src += [src['path']]

            if normal_src:
                tgt_data['fileGroups'] += [{
                    'isGenerated': False,
                    'sources': normal_src,
                }]
            if generated_src:
                tgt_data['fileGroups'] += [{
                    'isGenerated': True,
                    'sources': generated_src,
                }]
            return tgt_data

        def parse_project(pro: dict) -> dict:
            # Only look at the first directory specified in directoryIndexes
            # TODO Figure out what the other indexes are there for
            p_src_dir = source_dir
            p_bld_dir = build_dir
            try:
                p_src_dir, p_bld_dir = helper_parse_dir(cnf['directories'][pro['directoryIndexes'][0]])
            except (IndexError, KeyError):
                pass

            pro_data = {
                'name': pro.get('name', ''),
                'sourceDirectory': p_src_dir,
                'buildDirectory': p_bld_dir,
                'targets': [],
            }

            for ref in pro.get('targetIndexes', []):
                tgt = {}
                try:
                    tgt = cnf['targets'][ref]
                except (IndexError, KeyError):
                    pass
                pro_data['targets'] += [parse_target(tgt)]

            return pro_data

        for cnf in data.get('configurations', []):
            cnf_data = {
                'name': cnf.get('name', ''),
                'projects': [],
            }

            for pro in cnf.get('projects', []):
                cnf_data['projects'] += [parse_project(pro)]

            self.cmake_configurations += [CMakeConfiguration(cnf_data)]

    def _parse_cmakeFiles(self, data: dict) -> None:
        assert('inputs' in data)
        assert('paths' in data)

        src_dir = data['paths']['source']

        for i in data['inputs']:
            path = i['path']
            path = path if os.path.isabs(path) else os.path.join(src_dir, path)
            self.cmake_sources += [CMakeBuildFile(path, i.get('isCMake', False), i.get('isGenerated', False))]

    def _strip_data(self, data: Any) -> Any:
        if isinstance(data, list):
            for idx, i in enumerate(data):
                data[idx] = self._strip_data(i)

        elif isinstance(data, dict):
            new = {}
            for key, val in data.items():
                if key not in STRIP_KEYS:
                    new[key] = self._strip_data(val)
            data = new

        return data

    def _resolve_references(self, data: Any) -> Any:
        if isinstance(data, list):
            for idx, i in enumerate(data):
                data[idx] = self._resolve_references(i)

        elif isinstance(data, dict):
            # Check for the "magic" reference entry and insert
            # it into the root data dict
            if 'jsonFile' in data:
                data.update(self._reply_file_content(data['jsonFile']))

            for key, val in data.items():
                data[key] = self._resolve_references(val)

        return data

    def _reply_file_content(self, filename: str) -> dict:
        real_path = os.path.join(self.reply_dir, filename)
        if not os.path.exists(real_path):
            raise CMakeException('File "{}" does not exist'.format(real_path))

        with open(real_path, 'r') as fp:
            return json.load(fp)
