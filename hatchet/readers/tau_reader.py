#!/usr/bin/env python3
"""TAU trial data for TAU Profile.x.y.z format profiles

Parses a set of TAU profile files and yields multi-indexed Pandas dataframes for the
interval and atomic events.
"""
from __future__ import print_function
import glob
import mmap
import os
import re
import xml.etree.ElementTree as ElementTree
from sys import stderr

import pandas
import sys

class Node():
    """
    Abstract node of a tree that will be passed to hatchet API
    """
    def __init__(self, name, kwargs):
        raise NotImplemented("Node is abstract")
        
    def to_dict(self):
        """
        Hatchet expects a dictionary representation of each node with at least
        the keys `name`(string) and `metrics`(dict).
        an examplie of metrics would be : {"inclusive_time": 10.0, "exclusive_time": 9.0}
        """
        return {"name": self._name, "metrics": self._metrics}
    
    def _initialize(self, name, kwargs):
        """
        constructor, called by subclasses
        """
        self._name = name
        self._metrics = kwargs
            
class LeafNode(Node):
    """
    Just a node
    """
    def __init__(self, name, **kwargs):
        """
        name: str; name of the timer
        kwargs; metrics of the node
        """
        super(LeafNode, self)._initialize(name, kwargs)
            
class InnerNode(Node):
    """
    A node with children
    """
    def __init__(self, name, **kwargs):
        """
        name: str; name of the timer
        kwargs; metrics of the node
        """
        super(InnerNode, self)._initialize(name, kwargs)
        self._children = set()
        
    def to_dict(self):
        """
        Hatchet expects a inner nodes of the tree to contain, on top of what a regular node contains,
        one extra field called `children`.
        children are also nodes.
        """
        children = []
        for child in self._children:
            children.append(child.to_dict())
        return {"name": self._name, "metrics": self._metrics, "children": children}
    
    def add_children(self, node):
        self._children.add(node)
        

class CallPaths():
    """
    Generates call paths that are understood by hatchet
    """
    def __init__(self, non_call_path_data, call_path_data):
        """ initializer should not be directly called instead use factory method """
        self._roots = []
        self._non_call_path = non_call_path_data
        self._call_path_data = call_path_data
        self._depth = len(call_path.index.levshape)
        self._recursive_constructor(self._call_path_data, None, 0)
        
    def get_roots(self):
        """
        creates a json-like (list of dictionaries) that is understood by hatchet.GraphFrame.from_literal() method.
        """
        return [root.to_dict() for root in self._roots]

    def _recursive_constructor(self, call_path_data, parent_node, level):
        """recursively builds the tree"""
        functions_on_this_level = list(call_path_data.groupby(level=0).groups.keys())
        functions_on_this_level = [i.strip() for i in functions_on_this_level]
        for func in functions_on_this_level:
            if level == self._depth - 1 or func == 'NaN':
                node = LeafNode(func, exclusive_time=self._non_call_path.loc[func]['Exclusive'],
                                      inclusive_time=self._non_call_path.loc[func]['Inclusive'])
            else:
                node = InnerNode(func, exclusive_time=self._non_call_path.loc[func]['Exclusive'],
                                      inclusive_time=self._non_call_path.loc[func]['Inclusive'])
                self._recursive_constructor(call_path_data.loc[func], node, level + 1)

            if parent_node is not None:
                parent_node.add_children(node)
            else:
                self._roots.append(node)
                
    @staticmethod
    def _get_call_paths(data, node, context, thread):
        data = data[data['Group'].str.contains('TAU_CALLPATH', regex=False)].loc[node, context, thread]
        data = data.set_index(data.index.str.split("\s*=>\s*", expand=True))
        return data
        
    @staticmethod
    def _get_non_call_paths(data, node, context, thread):
        data = data[~data['Group'].str.contains('TAU_CALLPATH', regex=False)].loc[node, context, thread]
        data = data.set_index(data.index.str.strip())
        return data

    @staticmethod
    def from_tau_interval_profile(tau_interval, node, context, thread):
        """
        Creates and returns a CallPath object
        
        tau_interval: pandas.DataFrame; the interval data from TauProfileParser
        node: int
        contex: int
        thread: int
        """
        non_call_path_data = CallPaths._get_non_call_paths(tau_interval, node, context, thread)
        call_path_data = CallPaths._get_call_paths(tau_interval, node, context, thread)
        return CallPaths(non_call_path_data, call_path_data)

class TauProfileParser(object):
    """Parser for TAU's profile.* format."""

    _interval_header_re = re.compile(b'(\\d+) templated_functions_MULTI_(.+)')

    _atomic_header_re = re.compile(b'(\\d+) userevents')

    def __init__(self, trial, metric, metadata, indices, interval_data, atomic_events):
        self.trial = trial
        self.metric = metric
        self.metadata = metadata
        self.indices = indices
        self._interval_data = interval_data
        self._atomic_data = atomic_events

    def interval_data(self):
        return self._interval_data

    def atomic_data(self):
        return self._atomic_data

    def get_value_types(self):
        return [key for key in dict(self._interval_data.dtypes)
                if dict(self._interval_data.dtypes)[key] in ['float64', 'int64']]

    def summarize_samples(self, across_threads=False, callpaths=True):
        groups = 'Timer Name' if across_threads else ['Node', 'Context', 'Thread', 'Timer Name']
        if callpaths:
            base_data = self._interval_data.loc[self._interval_data['Group'].str.contains("TAU_SAMPLE")]
        else:
            base_data = self._interval_data.loc[self._interval_data['Timer Type'] == 'SAMPLE']
        summary = base_data.groupby(groups).sum()
        summary.index = summary.index.map(
            lambda x: '[SUMMARY] ' + x if across_threads else (x[0], x[1], x[2], '[SUMMARY] ' + x[3]))
        return summary

    def summarize_allocations(self):
        sums = self.atomic_data().groupby('Timer').agg({'Count': 'sum', 'Mean': 'mean'})
        allocs = sums[sums.index.to_series().str.contains('alloc')][['Count', 'Mean']]
        allocs['Total'] = allocs['Count'] * allocs['Mean']
        return allocs

    @classmethod
    def _parse_header(cls, fin):
        match = cls._interval_header_re.match(fin.readline())
        interval_count, metric = match.groups()
        return int(interval_count), metric

    @classmethod
    def _parse_metadata(cls, fin):
        fields, xml_wanabe = fin.readline().split(b'<metadata>')
        xml_wanabe = b'<metadata>' + xml_wanabe
        if (fields != b"# Name Calls Subrs Excl Incl ProfileCalls" and
                fields != b'# Name Calls Subrs Excl Incl ProfileCalls # '):
            raise RuntimeError('Invalid profile file: %s' % fin.name)
        try:
            metadata_tree = ElementTree.fromstring(xml_wanabe)
        except ElementTree.ParseError as err:
            raise RuntimeError('Invalid profile file: %s' % err)
        metadata = {}
        for attribute in metadata_tree.iter('attribute'):
            name = attribute.find('name').text
            value = attribute.find('value').text
            metadata[name] = value
        return metadata

    @classmethod
    def _parse_interval_data(cls, fin, count):
        pass

    @classmethod
    def _parse_atomic_header(cls, fin):
        aggregates = fin.readline().split(b' aggregates')[0]
        if aggregates != b'0':
            print("aggregates != 0, is '%s'" % aggregates, file=stderr)
        match = cls._atomic_header_re.match(fin.readline())
        try:
            count = int(match.group(1))
            if fin.readline() != b"# eventname numevents max min mean sumsqr\n":
                raise RuntimeError('Invalid profile file: %s' % fin.name)
        except AttributeError:
            count = 0
        return count

    @staticmethod
    def extract_from_timer_name(name):
        import re
        tag_search = re.search('^\[(\w+)\]\s+(.*)', name)
        timer_type, rest = tag_search.groups() if tag_search else (None, name)
        name_search = re.search('(.+)\[({.*)\]', rest)
        func_name, location = name_search.groups() if name_search else (rest, None)
        return func_name, location, timer_type

    @classmethod
    def parse(cls, dir_path, filenames=None, trial=None, MULTI=False):
        # default behavior is to run the profile* files first, if multi=true then it will look for MULTI__ folders
        if MULTI:
            return cls.multi_parse(dir_path, filenames, trial)

        if not os.path.isdir(dir_path):
            print("Error: %s is not a directory." % dir_path, file=stderr)
            sys.exit(1)

        if filenames is None:
            filenames = [os.path.basename(x) for x in glob.glob(os.path.join(dir_path, 'profile.*'))]
        if not filenames:
            multi_dir = [x for x in glob.glob(dir_path + '/MULTI*')]

        if filenames:
            return cls.profile_parse(dir_path, filenames, trial)
        elif multi_dir:
            return cls.multi_parse(dir_path, filenames, trial)
        else:
            print("Error: No Profile or MULTI__ to parse.")
            sys.exit(1)

    @classmethod
    def profile_parse(cls, dir_path, filenames=None, trial=None):
        intervals = []
        atomics = []
        indices = []
        trial_data_metric = None
        trial_data_metadata = None
        if filenames is None or filenames == []:
            filenames = [os.path.basename(x) for x in glob.glob(os.path.join(dir_path, 'profile.*'))]
        if not filenames:
            print("Error: No profile files found.")
            sys.exit(1)
        for filename in sorted(filenames,
                               key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split('(\d+)', s)]):
            location = os.path.basename(filename).replace('profile.', '')
            node, context, thread = (int(x) for x in location.split('.'))
            file_path = os.path.join(dir_path, filename)
            with open(file_path) as fin:
                mm = mmap.mmap(fin.fileno(), 0, mmap.MAP_PRIVATE, mmap.PROT_READ)
                interval_count, metric = cls._parse_header(mm)
                if not trial_data_metric:
                    trial_data_metric = metric
                metadata = cls._parse_metadata(mm)
                if not trial_data_metadata:
                    trial_data_metadata = metadata
                interval = pandas.read_csv(mm, nrows=interval_count, delim_whitespace=True,
                                           names=['Calls', 'Subcalls', 'Exclusive',
                                                  'Inclusive', 'ProfileCalls', 'Group'],
                                           engine='c')
                split_index = interval.reset_index()['index'].apply(cls.extract_from_timer_name)
                for n, col in enumerate(['Timer Name', 'Timer Location', 'Timer Type']):
                    interval[col] = split_index.apply(lambda l: l[n]).values
                mm.seek(0)
                for i in range(0, interval_count + 2):
                    mm.readline()
                cls._parse_atomic_header(mm)
                atomic = pandas.read_csv(mm, names=['Count', 'Maximum', 'Minimum', 'Mean', 'SumSq'],
                                         delim_whitespace=True, engine='c')
                mm.close()
                intervals.append(interval)
                atomics.append(atomic)
                indices.append((node, context, thread))

        interval_df = pandas.concat(intervals, keys=indices)
        interval_df.index.rename(['Node', 'Context', 'Thread', 'Timer'], inplace=True)
        atomic_df = pandas.concat(atomics, keys=indices)
        atomic_df.index.rename(['Node', 'Context', 'Thread', 'Timer'], inplace=True)
        return cls(trial, trial_data_metric, trial_data_metadata, indices, interval_df, atomic_df)

    @classmethod
    def multi_parse(cls, path_to_multis, filenames=None, trial=None):
        multi_dir = [x for x in glob.glob(path_to_multis + '/MULTI*')]
        tau_objs = [cls.profile_parse(folder, filenames, trial) for folder in multi_dir]
        combined_metric = b', '.join([tau_obj.metric for tau_obj in tau_objs])
        combined_metadata = tau_objs[0].metadata
        combined_metadata['Metric Name'] = ', '.join([tau_obj.metadata['Metric Name'] for tau_obj in tau_objs])
        combined_indices = tau_objs[0].indices
        combined_atomic_df = tau_objs[0].atomic_data()

        combined_intervals = pandas.concat({"": tau_objs[0].interval_data().drop(['Exclusive', 'Inclusive'], axis=1)},
                                           axis=1,
                                           names=['Metric', 'Intervals']).swaplevel('Metric', 'Intervals', axis=1)

        # build Exclusive and inclusive dictionaires to do line 250
        # exclusives

        # in_between_exclusive_df = pandas.concat([tau_obj.interval_data()['Exclusive'].to_frame().rename(columns={
        # 'Exclusive':combined_metadata['Metric Name'].split(', ')[tau_objs.index(tau_obj)]}) for #tau_obj in
        # tau_objs], axis=1)

        exclusive_df = pandas.concat({'Exclusive': pandas.concat([tau_obj.interval_data()[
                                                                      'Exclusive'].to_frame().rename(
            columns={'Exclusive': combined_metadata['Metric Name'].split(', ')[tau_objs.index(tau_obj)]}) for tau_obj in
                                                                  tau_objs], axis=1)}, axis=1,
                                     names=['Intervals', 'Metric'])

        # inclusives
        inclusive_df = pandas.concat({'Inclusive': pandas.concat([tau_obj.interval_data()[
                                                                      'Inclusive'].to_frame().rename(
            columns={'Inclusive': combined_metadata['Metric Name'].split(', ')[tau_objs.index(tau_obj)]}) for tau_obj in
                                                                  tau_objs], axis=1)}, axis=1,
                                     names=['Intervals', 'Metric'])
        combined_intervals = pandas.concat([combined_intervals, exclusive_df, inclusive_df], axis=1)

        return cls(trial, combined_metric, combined_metadata, combined_indices, combined_intervals, combined_atomic_df)


    def read(self):
        


