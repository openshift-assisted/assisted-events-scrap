from dataclasses import dataclass
from typing import List
from copy import deepcopy
import queue
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import dpath.util
from dpath.exceptions import PathNotFound
from retry import retry
from utils import ErrorCounter, Changes, log, get_event_id, get_dict_hash
from storage import ClusterEventsStorage, ElasticsearchStorage
from events_scrape import InventoryClient
from sentry_sdk import capture_exception
from config import SentryConfig, EventStoreConfig
from assisted_service_client.rest import ApiException

EVENT_CATEGORIES = ["user", "metrics"]


@dataclass
class ClusterEventsWorkerConfig:
    max_workers: int
    sentry: SentryConfig
    error_counter: ErrorCounter
    changes: Changes
    events: EventStoreConfig


class ClusterEventsWorker:
    def __init__(self, config: ClusterEventsWorkerConfig, ai_client: InventoryClient,
                 cluster_events_storage: ClusterEventsStorage, es_store: ElasticsearchStorage):

        self._es_store = es_store
        self._ai_client = ai_client
        self.cluster_events_storage = cluster_events_storage
        self._config = config
        self._executor = None
        self._es_store = es_store

    def process_clusters(self, clusters: List[dict]) -> None:
        with ThreadPoolExecutor(max_workers=self._config.max_workers) as self._executor:
            cluster_count = len(clusters)
            for cluster in clusters:
                self._executor.submit(self.store_events_for_cluster, cluster)
            log.info(f"Sent {cluster_count} clusters for processing...")

    def store_events_for_cluster(self, cluster: dict) -> None:
        try:
            log.debug(f"Storing cluster: {cluster}")
            if "hosts" not in cluster or len(cluster["hosts"]) == 0:
                cluster["hosts"] = self.__get_hosts(cluster["id"])
            events = self.__get_events(cluster["id"])
            component_versions = self.__get_versions()
            self._store_normalized_events(component_versions, cluster, events)
            self.cluster_events_storage.store(component_versions, cluster, events)
            self._config.changes.set_changed()
            log.debug(f'Storing events for cluster {cluster["id"]}')
        except Exception as e:
            self.__handle_unexpected_error(e, f'Error while processing cluster {cluster["id"]}')

    @retry(ApiException, delay=1, tries=3, backoff=2, max_delay=4, jitter=1)
    def __get_versions(self):
        return self._ai_client.get_versions()

    @retry(ApiException, delay=1, tries=3, backoff=2, max_delay=4, jitter=1)
    def __get_events(self, cluster_id: str):
        events = []
        try:
            events = self._ai_client.get_events(cluster_id, categories=EVENT_CATEGORIES)
        except ApiException as e:
            if e.status != 404:
                raise e
            log.debug(f'Events for cluster {cluster_id} not found')
        return events

    @retry(ApiException, delay=1, tries=3, backoff=2, max_delay=4, jitter=1)
    def __get_hosts(self, cluster_id: str):
        hosts = []
        try:
            hosts = self._ai_client.get_cluster_hosts(cluster_id=cluster_id)
        except ApiException as e:
            if e.status != 404:
                raise e
            # If a cluster is not found, then we consider to have 0 hosts. It was probably deleted
            log.debug(f'Cluster {cluster_id} not found while retrieving hosts')
        return hosts

    def __handle_unexpected_error(self, e: Exception, msg: str):
        self._config.error_counter.inc()
        if self._config.sentry.enabled:
            capture_exception(e)
        log.exception(msg)

    def shutdown(self):
        """
        This is needed for python 3.8 and lower. With python 3.9 we can pass a parameter:
        self._executor.shutdown(wait=False, cancel_futures=True)
        """
        if self._executor:
            # Do not accept further tasks
            self._executor.shutdown(wait=False)
            self._drain_queue()

    def _drain_queue(self):
        while True:
            try:
                work_item = self._executor._work_queue.get_nowait()  # pylint: disable=protected-access
            except queue.Empty:
                break
            if work_item:
                work_item.future.cancel()

    def _store_normalized_events(self, component_versions, cluster, event_list):
        try:
            cluster_id_filter = {
                "term": {
                    "cluster_id": cluster["id"]
                }
            }
            self._es_store.store_changes(
                index=EventStoreConfig.COMPONENT_VERSIONS_EVENTS_INDEX,
                documents=[component_versions],
                id_fn=get_dict_hash,
                transform_document_fn=add_timestamp
            )
            self._es_store.store_changes(
                index=EventStoreConfig.CLUSTER_EVENTS_INDEX,
                documents=[cluster],
                id_fn=self._cluster_checksum,
                filter_by=cluster_id_filter)
            self._es_store.store_changes(
                index=EventStoreConfig.EVENTS_INDEX,
                documents=event_list,
                id_fn=get_event_id,
                filter_by=cluster_id_filter)
        except Exception as e:
            self.__handle_unexpected_error(e, f'Error while storing normalized events for cluster {cluster["id"]}')

    def _cluster_checksum(self, doc: dict) -> dict:
        doc_copy = deepcopy(doc)
        for field in self._config.events.cluster_events_ignore_fields:
            try:
                dpath.util.delete(doc_copy, field, separator=".")
            except PathNotFound:
                # if field is not there, no need to delete it, but don't fail
                pass
        if "hosts" in doc_copy:
            doc_copy["hosts"].sort(key=by_id)
        return get_dict_hash(doc_copy)


def by_id(item: dict) -> str:
    return item.get("id", None)


def add_timestamp(doc: dict) -> dict:
    doc["timestamp"] = datetime.utcnow().isoformat()
    return doc
