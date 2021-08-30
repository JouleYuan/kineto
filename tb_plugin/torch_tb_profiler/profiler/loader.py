
# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# --------------------------------------------------------------------------
import bisect
import os
import sys
from collections import defaultdict

from .. import consts, io, utils
from ..run import Run
from .multiprocessing import Process, Queue
from .data import DistributedRunProfileData, RunProfileData
from .run_generator import DistributedRunGenerator, RunGenerator

logger = utils.get_logger()


class RunLoader(object):
    def __init__(self, name, run_dir, caches):
        self.run_name = name
        self.run_dir = run_dir
        self.caches = caches
        self.queue = Queue()

    def load(self):
        workers = []
        spans_by_workers = defaultdict(list)
        for path in io.listdir(self.run_dir):
            if io.isdir(io.join(self.run_dir, path)):
                continue
            match = consts.WORKER_PATTERN.match(path)
            if not match:
                continue

            worker = match.group(1)
            span = match.group(2)
            if span is not None:
                # remove the starting dot (.)
                span = span[1:]
                bisect.insort(spans_by_workers[worker], span)

            workers.append((worker, span, path))

        span_index_map = {}
        for worker, span_array in spans_by_workers.items():
            for i, span in enumerate(span_array, 1):
                span_index_map[(worker, span)] = i

        for worker, span, path in workers:
            # convert the span timestamp to the index.
            span_index = None if span is None else span_index_map[(worker, span)]
            p = Process(target=self._process_data, args=(worker, span_index, path))
            p.start()
        logger.info("started all processing")

        distributed_run = Run(self.run_name, self.run_dir)
        run = Run(self.run_name, self.run_dir)
        num_items = len(workers)
        while num_items > 0:
            item = self.queue.get()
            num_items -= 1
            r, d = item
            if r or d:
                logger.debug("Loaded profile via mp.Queue")
            if r is not None:
                run.add_profile(r)
            if d is not None:
                distributed_run.add_profile(d)

        distributed_profiles = self._process_spans(distributed_run)
        for d in distributed_profiles:
            if d is not None:
                run.add_profile(d)

        # for no daemon process, no need to join them since it will automatically join
        return run

    def _process_data(self, worker, span, path):
        import absl.logging
        absl.logging.use_absl_handler()

        try:
            logger.debug("Parse trace, run_dir=%s, worker=%s", self.run_dir, path)
            local_file = self.caches.get_remote_cache(io.join(self.run_dir, path))
            data, trace_path = RunProfileData.parse(worker, span, local_file)
            if trace_path != local_file:
                self.caches.add_file(local_file, trace_path)

            generator = RunGenerator(worker, span, data)
            profile = generator.generate_run_profile()
            dist_data = DistributedRunProfileData(data)

            logger.debug("Sending back profile via mp.Queue")
            self.queue.put((profile, dist_data))
        except KeyboardInterrupt:
            logger.warning("tb_plugin receive keyboard interrupt signal, process %d will exit" % (os.getpid()))
            sys.exit(1)
        except Exception as ex:
            logger.warning("Failed to parse profile data for Run %s on %s. Exception=%s",
                               self.run_name, worker, ex, exc_info=True)
            self.queue.put((None, None))
        logger.debug("finishing process data")

    def _process_spans(self, distributed_run):
        spans = distributed_run.get_spans()
        if spans is None:
            return [self._process_distributed_profiles(distributed_run.get_profiles(), None)]
        else:
            span_profiles = []
            for span in spans:
                profiles = distributed_run.get_profiles(span=span)
                p = self._process_distributed_profiles(profiles, span)
                if p is not None:
                    span_profiles.append(p)
            return span_profiles

    def _process_distributed_profiles(self, profiles, span):
        has_communication = True
        comm_node_lists = []
        logger.debug("Processing profile data")
        min_id = 0
        max_id = float('inf')
        for data in profiles:
            # Set has_communication to False and disable distributed view if any one worker has no communication
            if data.has_communication and data.comm_node_list:
                comm_node_lists.append(data.comm_node_list)
                cur_min_id = float('inf')
                cur_max_id = 0
                for comm_node in comm_node_lists[-1]:
                    cur_min_id = min(cur_min_id, comm_node.comm_id)
                    cur_max_id = max(cur_max_id, comm_node.comm_id)
                min_id = max(min_id, cur_min_id)
                max_id = min(max_id, cur_max_id)
            else:
                has_communication = False
                break
        if has_communication:
            for i in range(len(comm_node_lists)):
                j = 0
                while j < len(comm_node_lists[i]):
                    if min_id <= comm_node_lists[i][j].comm_id <= max_id:
                        j += 1
                    else:
                        comm_node_lists[i].pop(j)
                if len(comm_node_lists[i]) != len(comm_node_lists[0]):
                    logger.error("Number of communication operation nodes don't match between workers in run: %s" % self.run_name)
                    has_communication = False
                    break
        logger.debug("Processing profile data finish")

        if not has_communication:
            logger.debug("There is no communication profile in this run.")
            return None

        worker_num = len(comm_node_lists)
        i = len(comm_node_lists[0]) - 1
        while i >= 0:
            ragged_kernel = False
            for j in range(1, worker_num):
                if len(comm_node_lists[0][i].kernel_ranges) != len(comm_node_lists[j][i].kernel_ranges):
                    ragged_kernel = True
                    break
            if ragged_kernel:
                for j in range(worker_num):
                    comm_node_lists[j].pop(i)
            else:
                break
            i -= 1

        for i, node in enumerate(comm_node_lists[0]):
            kernel_range_size = len(node.kernel_ranges)
            # loop for all communication kernel ranges in order
            for j in range(kernel_range_size):
                min_range = sys.maxsize
                # For each kernel_range, find the minist between workers as the real communication time
                for k in range(worker_num):
                    kernel_ranges = comm_node_lists[k][i].kernel_ranges
                    if len(kernel_ranges) != kernel_range_size:
                        logger.error("Number of communication kernels don't match between workers in run: %s" % self.run_name)
                        has_communication = False
                        return None
                    if kernel_ranges:
                        if kernel_ranges[j][1] - kernel_ranges[j][0] < min_range:
                            min_range = kernel_ranges[j][1] - kernel_ranges[j][0]
                for k in range(worker_num):
                    kernel_range = comm_node_lists[k][i].kernel_ranges[j]
                    comm_node_lists[k][i].real_time_ranges.append((kernel_range[1] - min_range, kernel_range[1]))

        for data in profiles:
            data.communication_parse()

        generator = DistributedRunGenerator(profiles, span)
        profile = generator.generate_run_profile()
        return profile
