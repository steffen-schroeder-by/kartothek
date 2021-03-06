"""
Eager IO aka "everything is done locally and immediately".
"""
import logging
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

import pandas as pd
import simplekv
from simplekv import KeyValueStore

from kartothek.api.consistency import get_cube_payload
from kartothek.api.discover import discover_datasets, discover_datasets_unchecked
from kartothek.core.cube.conditions import Condition, Conjunction
from kartothek.core.cube.constants import (
    KTK_CUBE_DF_SERIALIZER,
    KTK_CUBE_METADATA_STORAGE_FORMAT,
    KTK_CUBE_METADATA_VERSION,
    KTK_CUBE_UUID_SEPERATOR,
)
from kartothek.core.cube.cube import Cube
from kartothek.core.dataset import DatasetMetadata
from kartothek.core.typing import StoreFactory
from kartothek.io.eager import (
    copy_dataset,
    store_dataframes_as_dataset,
    update_dataset_from_dataframes,
)
from kartothek.io_components.cube.append import check_existing_datasets
from kartothek.io_components.cube.cleanup import get_keys_to_clean
from kartothek.io_components.cube.common import assert_stores_different
from kartothek.io_components.cube.copy import get_datasets_to_copy
from kartothek.io_components.cube.query import load_group, plan_query, quick_concat
from kartothek.io_components.cube.remove import (
    prepare_metapartitions_for_removal_action,
)
from kartothek.io_components.cube.stats import (
    collect_stats_block,
    get_metapartitions_for_stats,
    reduce_stats,
)
from kartothek.io_components.cube.write import (
    MultiTableCommitAborted,
    apply_postwrite_checks,
    check_datasets_prebuild,
    check_datasets_preextend,
    check_provided_metadata_dict,
    multiplex_user_input,
    prepare_data_for_ktk,
    prepare_ktk_metadata,
    prepare_ktk_partition_on,
)
from kartothek.io_components.update import update_dataset_from_partitions
from kartothek.serialization._parquet import ParquetSerializer
from kartothek.utils.ktk_adapters import get_dataset_keys, metadata_factory_from_dataset
from kartothek.utils.pandas import concat_dataframes

logger = logging.getLogger()

__all__ = (
    "append_to_cube",
    "build_cube",
    "cleanup_cube",
    "collect_stats",
    "copy_cube",
    "delete_cube",
    "extend_cube",
    "query_cube",
    "remove_partitions",
)


def build_cube(
    data: Union[
        pd.DataFrame,
        Dict[str, pd.DataFrame],
        List[Union[pd.DataFrame, Dict[str, pd.DataFrame]]],
    ],
    cube: Cube,
    store: KeyValueStore,
    metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    overwrite: bool = False,
    partition_on: Optional[Dict[str, Iterable[str]]] = None,
    df_serializer: Optional[ParquetSerializer] = None,
) -> Dict[str, DatasetMetadata]:
    """
    Store given dataframes as Ktk_cube cube.

    ``data`` can be formatted in multiple ways:

    - single DataFrame::

          pd.DataFrame({
              'x': [0, 1, 2, 3],
              'p': [0, 0, 1, 1],
              'v': [42, 45, 20, 10],
          })

      In that case, the seed dataset will be written.

    - dictionary of DataFrames::

          {
              'seed': pd.DataFrame({
                  'x': [0, 1, 2, 3],
                  'p': [0, 0, 1, 1],
                  'v1': [42, 45, 20, 10],
              }),
              'enrich': pd.DataFrame({
                  'x': [0, 1, 2, 3],
                  'p': [0, 0, 1, 1],
                  'v2': [False, False, True, False],
              }),
          }

      In that case, multiple datasets can be written at the same time. Note that the seed dataset MUST be included.

    - list of anything above::

          [
              # seed data only
              pd.DataFrame({
                  'x': [0, 1, 2, 3],
                  'p': [0, 0, 1, 1],
                  'v1': [42, 45, 20, 10],
              }),
              # seed data only, explicit way
              {
                  'seed': pd.DataFrame({
                      'x': [4, 5, 6, 7],
                      'p': [0, 0, 1, 1],
                      'v1': [12, 32, 22, 9],
                  }),
              },
              # multiple datasets
              {
                  'seed': pd.DataFrame({
                      'x': [8, 9, 10, 11],
                      'p': [0, 0, 1, 1],
                      'v1': [9, 2, 4, 11],
                  }),
                  'enrich': pd.DataFrame({
                      'x': [8, 9, 10, 11],
                      'p': [0, 0, 1, 1],
                      'v2': [True, True, False, False],
                  }),
              },
              # non-seed data only
              {
                  'enrich': pd.DataFrame({
                      'x': [1, 2, 3, 4],
                      'p': [0, 0, 1, 1],
                      'v2': [False, True, False, False],
                  }),
              },
          ]

      In that case, multiple datasets may be written. Note that at least a single list element must contain seed data.

    Extra metdata may be preserved w/ every dataset, e.g.::

        {
            'seed': {
                'source': 'db',
                'host': 'db1.cluster20.company.net',
                'last_event': '230c6edb-b69a-4d30-b56d-28f5dfe20948',
            },
            'enrich': {
                'source': 'python',
                'commit_hash': '8b5d717518439921e6d17c7495956bdad687bc54',
            },
        }

    Note that the given data must be JSON-serializable.

    If the cube already exists, the ``overwrite`` flag must be given. In that case, all datasets that are part of the
    existing cube must be overwritten. Partial overwrites are not allowed.

    Parameters
    ----------
    data:
        Data that should be written to the cube. If only a single dataframe is given, it is assumed to be the seed
        dataset.
    cube:
        Cube specification.
    store:
        Store to which the data should be written to.
    metadata:
        Metadata for every dataset.
    overwrite:
        If possibly existing datasets should be overwritten.
    partition_on:
        Optional parition-on attributes for datasets (dictionary mapping :term:`Dataset ID` -> columns).
    df_serializer:
        Optional Dataframe to Parquet serializer

    Returns
    -------
    datasets: Dict[str, kartothek.core.dataset.DatasetMetadata]
        DatasetMetadata for every dataset written.
    """
    data = _normalize_user_input(data, cube)
    ktk_cube_dataset_ids = set(data.keys())
    prep_partition_on = prepare_ktk_partition_on(
        cube, ktk_cube_dataset_ids, partition_on
    )
    metadata = check_provided_metadata_dict(metadata, ktk_cube_dataset_ids)

    existing_datasets = discover_datasets_unchecked(cube.uuid_prefix, store)
    check_datasets_prebuild(data, cube, existing_datasets)

    # do all data preparation before writing anything
    data = _prepare_data_for_ktk_all(
        data=data, cube=cube, existing_payload=set(), partition_on=prep_partition_on
    )

    datasets = {}
    for ktk_cube_dataset_id, part in data.items():
        datasets[ktk_cube_dataset_id] = store_dataframes_as_dataset(
            store=store,
            dataset_uuid=cube.ktk_dataset_uuid(ktk_cube_dataset_id),
            dfs=part,
            metadata=prepare_ktk_metadata(cube, ktk_cube_dataset_id, metadata),
            partition_on=list(prep_partition_on[ktk_cube_dataset_id]),
            metadata_storage_format=KTK_CUBE_METADATA_STORAGE_FORMAT,
            metadata_version=KTK_CUBE_METADATA_VERSION,
            df_serializer=df_serializer or KTK_CUBE_DF_SERIALIZER,
            overwrite=overwrite,
        )

    return apply_postwrite_checks(
        datasets=datasets, cube=cube, store=store, existing_datasets=existing_datasets
    )


def extend_cube(
    data: Union[
        pd.DataFrame,
        Dict[str, pd.DataFrame],
        List[Union[pd.DataFrame, Dict[str, pd.DataFrame]]],
    ],
    cube: Cube,
    store: KeyValueStore,
    metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    overwrite: bool = False,
    partition_on: Optional[Dict[str, Iterable[str]]] = None,
    df_serializer: Optional[ParquetSerializer] = None,
) -> Dict[str, DatasetMetadata]:
    """
    Store given dataframes into an existing Kartothek cube.

    For details on ``data`` and ``metadata``, see :meth:`build_cube`.

    Parameters
    ----------
    data:
        Data that should be written to the cube. If only a single dataframe is given, it is assumed to be the seed
        dataset.
    cube:
        Cube specification.
    store:
        Store to which the data should be written to.
    metadata:
        Metadata for every dataset.
    overwrite:
        If possibly existing datasets should be overwritten.
    partition_on:
        Optional parition-on attributes for datasets (dictionary mapping :term:`Dataset ID` -> columns).
    df_serializer:
        Optional Dataframe to Parquet serializer

    Returns
    -------
    datasets: Dict[str, kartothek.core.dataset.DatasetMetadata]
        DatasetMetadata for every dataset written.
    """
    data = _normalize_user_input(data, cube)
    ktk_cube_dataset_ids = set(data.keys())
    prep_partition_on = prepare_ktk_partition_on(
        cube, ktk_cube_dataset_ids, partition_on
    )
    metadata = check_provided_metadata_dict(metadata, ktk_cube_dataset_ids)

    check_datasets_preextend(data, cube)

    existing_datasets = discover_datasets(cube, store)
    if overwrite:
        existing_datasets_cut = {
            ktk_cube_dataset_id: ds
            for ktk_cube_dataset_id, ds in existing_datasets.items()
            if ktk_cube_dataset_id not in data
        }
    else:
        existing_datasets_cut = existing_datasets
    existing_payload = get_cube_payload(existing_datasets_cut, cube)

    # do all data preparation before writing anything
    data = _prepare_data_for_ktk_all(
        data=data,
        cube=cube,
        existing_payload=existing_payload,
        partition_on=prep_partition_on,
    )

    datasets = {}
    for ktk_cube_dataset_id, part in data.items():
        datasets[ktk_cube_dataset_id] = store_dataframes_as_dataset(
            store=store,
            dataset_uuid=cube.ktk_dataset_uuid(ktk_cube_dataset_id),
            dfs=part,
            metadata=prepare_ktk_metadata(cube, ktk_cube_dataset_id, metadata),
            partition_on=list(prep_partition_on[ktk_cube_dataset_id]),
            metadata_storage_format=KTK_CUBE_METADATA_STORAGE_FORMAT,
            metadata_version=KTK_CUBE_METADATA_VERSION,
            df_serializer=df_serializer or KTK_CUBE_DF_SERIALIZER,
            overwrite=overwrite,
        )

    return apply_postwrite_checks(
        datasets=datasets, cube=cube, store=store, existing_datasets=existing_datasets
    )


def query_cube(
    cube,
    store,
    conditions=None,
    datasets=None,
    dimension_columns=None,
    partition_by=None,
    payload_columns=None,
):
    """
    Query cube.

    .. note::
        In case of ``partition_by=None`` (default case), only a single partition is generated. If this one will be
        empty (e.g. due to the provided conditions), an empty list will be returned, and a single-element list
        otherwise.

    Parameters
    ----------
    cube: Cube
        Cube specification.
    store: simplekv.KeyValueStore
        KV store that preserves the cube.
    conditions: Union[None, Condition, Iterable[Condition], Conjunction]
        Conditions that should be applied, optional.
    datasets: Union[None, Iterable[str], Dict[str, kartothek.core.dataset.DatasetMetadata]]
        Datasets to query, must all be part of the cube. May be either the result of :func:`~kartothek.api.discover.discover_datasets`, a list
        of Ktk_cube dataset ID or ``None`` (in which case auto-discovery will be used).
    dimension_columns: Union[None, str, Iterable[str]]
        Dimension columns of the query, may result in projection. If not provided, dimension columns from cube
        specification will be used.
    partition_by: Union[None, str, Iterable[str]]
        By which column logical partitions should be formed. If not provided, a single partition will be generated.
    payload_columns: Union[None, str, Iterable[str]]
        Which columns apart from ``dimension_columns`` and ``partition_by`` should be returned.

    Returns
    -------
    dfs: List[pandas.DataFrame]
        List of non-empty DataFrames, order by ``partition_by``. Column of DataFrames is alphabetically ordered. Data
        types are provided on best effort (they are restored based on the preserved data, but may be different due to
        Pandas NULL-handling, e.g. integer columns may be floats).
    """
    intention, _empty, groups = plan_query(
        cube=cube,
        store=store,
        conditions=conditions,
        datasets=datasets,
        dimension_columns=dimension_columns,
        partition_by=partition_by,
        payload_columns=payload_columns,
    )
    dfs = [load_group(group=g, store=store, cube=cube) for g in groups]
    dfs = [df for df in dfs if not df.empty]
    if not intention.partition_by and (len(dfs) > 0):
        dfs = [
            quick_concat(
                dfs=dfs,
                dimension_columns=intention.dimension_columns,
                partition_columns=cube.partition_columns,
            )
        ]
    return dfs


def delete_cube(cube, store, datasets=None):
    """
    Delete cube from store.

    .. important::
        This routine only deletes tracked files. Garbage and leftovers from old cubes and failed operations are NOT
        removed.

    Parameters
    ----------
    cube: Cube
        Cube specification.
    store: Union[simplekv.KeyValueStore, Callable[[], simplekv.KeyValueStore]]
        KV store.
    datasets: Union[None, Iterable[str], Dict[str, kartothek.core.dataset.DatasetMetadata]]
        Datasets to delete, must all be part of the cube. May be either the result of :func:`~kartothek.api.discover.discover_datasets`, a list
        of Ktk_cube dataset ID or ``None`` (in which case entire cube will be deleted).
    """
    if callable(store):
        store = store()

    if not isinstance(datasets, dict):
        datasets = discover_datasets_unchecked(
            uuid_prefix=cube.uuid_prefix,
            store=store,
            filter_ktk_cube_dataset_ids=datasets,
        )

    keys = set()
    for ktk_cube_dataset_id in sorted(datasets.keys()):
        ds = datasets[ktk_cube_dataset_id]
        keys |= get_dataset_keys(ds)

    for k in sorted(keys):
        store.delete(k)


def _transform_uuid(
    src_uuid: str,
    cube_prefix: str,
    renamed_cube_prefix: Optional[str],
    renamed_datasets: Optional[Dict[str, str]],
):
    """
    Transform a uuid from <old cube prefix>++<old dataset> to
    <new cube prefix>++<new dataset>
    :param src_uuid:
        Uuid to transform
    :param cube_prefix:
        Cube prefix before renaming
    :param renamed_cube:
        Optional new cube prefix
    :param renamed_datasets:
        Optional dict of {old dataset name: new dataset name} entries to rename datasets
    """
    tgt_uuid = src_uuid
    if renamed_cube_prefix:
        tgt_uuid = src_uuid.replace(
            f"{cube_prefix}{KTK_CUBE_UUID_SEPERATOR}",
            f"{renamed_cube_prefix}{KTK_CUBE_UUID_SEPERATOR}",
        )

    if renamed_datasets:
        for ds_old, ds_new in renamed_datasets.items():
            if f"{KTK_CUBE_UUID_SEPERATOR}{ds_old}" in tgt_uuid:
                tgt_uuid = tgt_uuid.replace(
                    f"{KTK_CUBE_UUID_SEPERATOR}{ds_old}",
                    f"{KTK_CUBE_UUID_SEPERATOR}{ds_new}",
                )
    return tgt_uuid


def copy_cube(
    cube: Cube,
    src_store: Union[KeyValueStore, Callable[[], KeyValueStore]],
    tgt_store: Union[KeyValueStore, Callable[[], KeyValueStore]],
    overwrite: bool = False,
    datasets: Union[None, Iterable[str], Dict[str, DatasetMetadata]] = None,
    renamed_cube_prefix: Optional[str] = None,
    renamed_datasets: Optional[Dict[str, str]] = None,
):
    """
    Copy cube from one store to another.

    .. warning::
        A failing copy operation can not be rolled back if the `overwrite` flag is enabled
        and might leave the overwritten dataset in an inconsistent state.

    Parameters
    ----------
    cube: Cube
        Cube specification.
    src_store: Union[simplekv.KeyValueStore, Callable[[], simplekv.KeyValueStore]]
        Source KV store.
    tgt_store: Union[simplekv.KeyValueStore, Callable[[], simplekv.KeyValueStore]]
        Target KV store.
    overwrite: bool
        If possibly existing datasets in the target store should be overwritten.
    datasets: Union[None, Iterable[str], Dict[str, DatasetMetadata]]
        Datasets to copy, must all be part of the cube. May be either the result of :func:`~kartothek.api.discover.discover_datasets`, a list
        of Ktk_cube dataset ID or ``None`` (in which case entire cube will be copied).
    renamed_cube_prefix: Optional[str]
        Optional new cube prefix. If specified, the cube will be renamed while copying.
    renamed_datasets: Optional[Dict[str, str]]
        Optional dict with {old dataset name: new dataset name} entries. If provided,
        the datasets will be renamed accordingly during copying. When the parameter
        datasets is specified, the datasets to rename must be a subset of the datasets
        to copy.
    """
    if callable(src_store):
        src_store = src_store()
    if callable(tgt_store):
        tgt_store = tgt_store()
    assert_stores_different(
        src_store, tgt_store, cube.ktk_dataset_uuid(cube.seed_dataset)
    )
    existing_datasets = discover_datasets_unchecked(cube.uuid_prefix, tgt_store)

    if renamed_datasets is None:
        new_seed_dataset = cube.seed_dataset
    else:
        new_seed_dataset = renamed_datasets.get(cube.seed_dataset, cube.seed_dataset)

    new_cube = Cube(
        dimension_columns=cube.dimension_columns,
        partition_columns=cube.partition_columns,
        uuid_prefix=renamed_cube_prefix or cube.uuid_prefix,
        seed_dataset=new_seed_dataset,
        index_columns=cube.index_columns,
        suppress_index_on=cube.suppress_index_on,
    )

    datasets_to_copy = get_datasets_to_copy(
        cube=cube,
        src_store=src_store,
        tgt_store=tgt_store,
        overwrite=overwrite,
        datasets=datasets,
    )

    copied = {}  # type: Dict[str, DatasetMetadata]
    for src_ds_name, src_ds_meta in datasets_to_copy.items():
        tgt_ds_uuid = _transform_uuid(
            src_uuid=src_ds_meta.uuid,
            cube_prefix=cube.uuid_prefix,
            renamed_cube_prefix=renamed_cube_prefix,
            renamed_datasets=renamed_datasets,
        )
        try:
            md_transformed = copy_dataset(
                source_dataset_uuid=src_ds_meta.uuid,
                store=src_store,
                target_dataset_uuid=tgt_ds_uuid,
                target_store=tgt_store,
            )
        except Exception as e:
            if overwrite:
                # We can't roll back safely if the target dataset has been partially overwritten.
                raise RuntimeError(e)
            else:
                apply_postwrite_checks(
                    datasets=copied,
                    cube=new_cube,
                    store=tgt_store,
                    existing_datasets=existing_datasets,
                )
        else:
            copied.update(md_transformed)


def collect_stats(cube, store, datasets=None):
    """
    Collect statistics for given cube.

    Parameters
    ----------
    cube: Cube
        Cube specification.
    store: simplekv.KeyValueStore
        KV store that preserves the cube.
    datasets: Union[None, Iterable[str], Dict[str, kartothek.core.dataset.DatasetMetadata]]
        Datasets to query, must all be part of the cube. May be either the result of :func:`~kartothek.api.discover.discover_datasets`, a list
        of Ktk_cube dataset ID or ``None`` (in which case auto-discovery will be used).

    Returns
    -------
    stats: Dict[str, Dict[str, int]]
        Statistics per ktk_cube dataset ID.
    """
    if callable(store):
        store = store()

    if not isinstance(datasets, dict):
        datasets = discover_datasets_unchecked(
            uuid_prefix=cube.uuid_prefix,
            store=store,
            filter_ktk_cube_dataset_ids=datasets,
        )

    all_metapartitions = get_metapartitions_for_stats(datasets)
    return reduce_stats([collect_stats_block(all_metapartitions, store)])


def cleanup_cube(cube, store):
    """
    Remove unused keys from cube datasets.

    .. important::
        All untracked keys which start with the cube's `uuid_prefix` followed by the `KTK_CUBE_UUID_SEPERATOR`
        (e.g. `my_cube_uuid++seed...`) will be deleted by this routine. These keys may be leftovers from past
        overwrites or index updates.

    Parameters
    ----------
    cube: Cube
        Cube specification.
    store: Union[simplekv.KeyValueStore, Callable[[], simplekv.KeyValueStore]]
        KV store.
    """
    if callable(store):
        store = store()

    datasets = discover_datasets_unchecked(uuid_prefix=cube.uuid_prefix, store=store)
    keys = get_keys_to_clean(cube.uuid_prefix, datasets, store)

    for k in sorted(keys):
        store.delete(k)


def remove_partitions(
    cube: Cube,
    store: Union[simplekv.KeyValueStore, StoreFactory],
    conditions: Union[None, Condition, Sequence[Condition], Conjunction] = None,
    ktk_cube_dataset_ids: Optional[Union[Sequence[str], str]] = None,
    metadata: Optional[Dict[str, Dict[str, Any]]] = None,
):
    """
    Remove given partition range from cube using a transaction.

    Remove the partitions selected by ``conditions``. If no ``conditions`` are given,
    remove all partitions. For each considered dataset, only the subset of
    ``conditions`` that refers to the partition columns of the respective dataset
    is used. In particular, a dataset that is not partitioned at all is always considered
    selected by ``conditions``.

    Parameters
    ----------
    cube
        Cube spec.
    store
        Store.
    conditions
        Select the partitions to be removed. Must be a condition only on partition columns.
    ktk_cube_dataset_ids
        Ktk_cube dataset IDs to apply the remove action to, optional. Default to "all".
    metadata
        Metadata for every the datasets, optional. Only given keys are updated/replaced. Deletion of
        metadata keys is not possible.

    Returns
    -------
    datasets: Dict[str, kartothek.core.dataset.DatasetMetadata]
        Datasets, updated.
    """
    if callable(store):
        store_instance = store()
        store_factory = store
    else:
        store_instance = store

        def store_factory():
            return store

    existing_datasets = discover_datasets(cube, store)

    for (
        ktk_cube_dataset_id,
        (ds, mp, delete_scope),
    ) in prepare_metapartitions_for_removal_action(
        cube=cube,
        store=store_instance,
        conditions=conditions,
        ktk_cube_dataset_ids=ktk_cube_dataset_ids,
        existing_datasets=existing_datasets,
    ).items():
        mp = mp.store_dataframes(
            store=store_instance,
            dataset_uuid=ds.uuid,
            df_serializer=KTK_CUBE_DF_SERIALIZER,
        )

        ds_factory = metadata_factory_from_dataset(
            ds, with_schema=True, store=store_factory
        )

        existing_datasets[ktk_cube_dataset_id] = update_dataset_from_partitions(
            mp,
            store_factory=store_factory,
            dataset_uuid=ds.uuid,
            ds_factory=ds_factory,
            metadata=prepare_ktk_metadata(cube, ktk_cube_dataset_id, metadata),
            metadata_merger=None,
            delete_scope=delete_scope,
        )

    return existing_datasets


def append_to_cube(
    data: Union[
        pd.DataFrame,
        Dict[str, pd.DataFrame],
        List[Union[pd.DataFrame, Dict[str, pd.DataFrame]]],
    ],
    cube: Cube,
    store: KeyValueStore,
    metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    df_serializer: Optional[ParquetSerializer] = None,
) -> Dict[str, DatasetMetadata]:
    """
    Append data to existing cube.

    For details on ``data`` and ``metadata``, see :meth:`build_cube`.

    .. important::

        Physical partitions must be updated as a whole. If only single rows within a physical partition are updated, the
        old data is treated as "removed".

    .. hint::

        To have better control over the overwrite "mask" (i.e. which partitions are overwritten), you should use
        :meth:`remove_partitions` beforehand.

    Parameters
    ----------
    data:
        Data that should be written to the cube. If only a single dataframe is given, it is assumed to be the seed
        dataset.
    cube:
        Cube specification.
    store:
        Store to which the data should be written to.
    metadata:
        Metadata for every dataset, optional. For every dataset, only given keys are updated/replaced. Deletion of
        metadata keys is not possible.
    df_serializer:
        Optional Dataframe to Parquet serializer

    Returns
    -------
    datasets: Dict[str, kartothek.core.dataset.DatasetMetadata]
        DatasetMetadata for every dataset written.
    """
    data = _normalize_user_input(data, cube)

    existing_datasets = discover_datasets(cube, store)
    partition_on = {k: v.partition_keys for k, v in existing_datasets.items()}

    check_existing_datasets(
        existing_datasets=existing_datasets, ktk_cube_dataset_ids=set(data.keys())
    )

    # do all data preparation before writing anything
    # existing_payload is set to empty because we're not checking against any existing payload. ktk will account for the
    # compat check within 1 dataset
    data = _prepare_data_for_ktk_all(
        data=data, cube=cube, existing_payload=set(), partition_on=partition_on
    )

    # update_dataset_from_dataframes requires a store factory, so create one
    # if not provided
    if not callable(store):

        def store_factory():
            return store

    else:
        store_factory = store

    updated_datasets = {}
    for ktk_cube_dataset_id, part in data.items():
        updated_datasets[ktk_cube_dataset_id] = update_dataset_from_dataframes(
            store=store_factory,
            dataset_uuid=cube.ktk_dataset_uuid(ktk_cube_dataset_id),
            df_list=part,
            partition_on=list(partition_on[ktk_cube_dataset_id]),
            df_serializer=df_serializer or KTK_CUBE_DF_SERIALIZER,
            metadata=prepare_ktk_metadata(cube, ktk_cube_dataset_id, metadata),
        )

    return apply_postwrite_checks(
        datasets=updated_datasets,
        cube=cube,
        store=store,
        existing_datasets=existing_datasets,
    )


def _normalize_user_input(data, cube):
    if isinstance(data, (dict, pd.DataFrame)):
        data = [data]
    else:
        data = list(data)

    data_lists = defaultdict(list)
    for part in data:
        part = multiplex_user_input(part, cube)
        for k, v in part.items():
            data_lists[k].append(v)

    return {
        k: concat_dataframes([df for df in v if df is not None])
        for k, v in data_lists.items()
    }


def _prepare_data_for_ktk_all(data, cube, existing_payload, partition_on):
    data = {
        ktk_cube_dataset_id: prepare_data_for_ktk(
            df=df,
            ktk_cube_dataset_id=ktk_cube_dataset_id,
            cube=cube,
            existing_payload=existing_payload,
            partition_on=partition_on[ktk_cube_dataset_id],
        )
        for ktk_cube_dataset_id, df in data.items()
    }

    empty_datasets = {
        ktk_cube_dataset_id
        for ktk_cube_dataset_id, part in data.items()
        if part.is_sentinel
    }
    if empty_datasets:
        cause = ValueError(
            "Cannot write empty datasets: {empty_datasets}".format(
                empty_datasets=", ".join(sorted(empty_datasets))
            )
        )
        exc = MultiTableCommitAborted("Aborting commit.")
        exc.__cause__ = cause
        raise exc

    return data
