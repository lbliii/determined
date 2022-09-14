import abc
import contextlib
import json
import logging
import pathlib
import pickle
import random
import sys
import time
import warnings
from abc import abstractmethod
from inspect import signature
from typing import Any, Callable, Dict, Iterator, List, Optional, Type, Union, cast

import numpy as np
import torch
import torch.distributed as dist

import determined as det
from determined import core, pytorch, tensorboard, util, workload, TrialController
from determined.common import check
from determined.core import SearcherOperation
from determined.horovod import hvd
from determined.profiler import ProfilerAgent, DummyProfilerAgent
from determined.pytorch import PyTorchTrialContext
from determined.tensorboard.util import get_rank_aware_path
from determined.util import has_param

# Apex is included only for GPU trials.
try:
    import apex
except ImportError:  # pragma: no cover
    apex = None
    pass


class TrainUnit:
    def __init__(self, value):
        self.value = value

    @staticmethod
    def from_searcher_unit(length: int, unit: core.Unit):
        if unit == core.Unit.EPOCHS:
            return Epoch(length)
        elif unit == core.Unit.RECORDS:
            return Record(length)
        elif unit == core.Unit.BATCHES:
            return Batch(length)
        else:
            raise ValueError(f"unrecognized searcher unit {unit}")

    @staticmethod
    def from_values(batches: Optional[int] = None,
                    records: Optional[int] = None,
                    epochs: Optional[int] = None):
        if sum((batches is not None, records is not None, epochs is not None)) != 1:
            raise ValueError(f"invalid length: batches={batches} records={records} epochs={epochs}")
        if batches:
            return Batch(batches)
        elif records:
            return Record(records)
        elif epochs:
            return Epoch(epochs)


class Epoch(TrainUnit):
    pass


class Batch(TrainUnit):
    pass

class Record(TrainUnit):
    pass

class ShouldExit(Exception):
    """
    ShouldExit breaks out of the top-level train loop from inside function calls.
    """

    def __init__(self, skip_exit_checkpoint: bool = False):
        self.skip_exit_checkpoint = skip_exit_checkpoint


class TrialState:
    def __init__(
        self,
        trial_id: int = 0,
        last_ckpt: int = 0,
        steps_completed: int = 0,
        step_id: int = 0,
        last_val: int = 0,
        batches_trained: int = None,
        epochs_trained: int = None,
    ) -> None:
        # Store TrialID to distinguish between e.g. pause/restart and continue training.
        self.trial_id = trial_id
        self.last_ckpt = last_ckpt
        self.steps_completed = steps_completed
        self.step_id = step_id
        self.last_val = last_val
        self.batches_trained = batches_trained
        self.epochs_trained = epochs_trained


class PyTorchTrialController:
    def __init__(self, trial_inst: det.Trial,
                 context: pytorch.PyTorchTrialContext,
                 min_checkpoint_period: TrainUnit,
                 min_validation_period: TrainUnit,
                 average_training_metrics: Optional[bool] = True,
                 smaller_is_better: Optional[bool] = True,
                 max_length: Optional[TrainUnit] = None,
                 steps_completed: Optional[int] = 0,
                 latest_checkpoint: Optional[str] = None,
                 local_training: Optional[bool] = False,
                 test_mode: Optional[bool] = False,
                 scheduling_unit: Optional[int] = sys.maxsize,
                 det_profiler: Optional[ProfilerAgent] = DummyProfilerAgent(),
                 searcher_metric_name: Optional[str] = None,
                 debug: Optional[bool] = False,
                 checkpoint_policy: Optional[str] = "best",
                 ) -> None:

        if not isinstance(trial_inst, PyTorchTrial):
            raise TypeError("PyTorchTrialController requires a PyTorchTrial.")
        self.trial = trial_inst
        self.context = context
        self.core_context = self.context._core
        self.prof = det_profiler
        self.context._set_determined_profiler(self.prof)

        self.local_training = local_training

        distributed_backend = det._DistributedBackend()
        self.use_horovod = distributed_backend.use_horovod()
        self.use_torch = distributed_backend.use_torch()
        self.is_chief = self.context.distributed.rank == 0

        # Training loop variables
        self.max_length = max_length
        self.min_checkpoint_period = min_checkpoint_period
        self.min_validation_period = min_validation_period
        self.scheduling_unit = scheduling_unit

        # Training loop state
        if local_training:
            self._trial_id = 0
            assert self.max_length, "max_length must be specified for local-training mode"
        else:
            self._trial_id = self.core_context.train._trial_id

        self.state = TrialState(trial_id=self._trial_id,
                                batches_trained=steps_completed)
        self.searcher_op = None
        self._searcher_unit = self.core_context.searcher.get_configured_units()
        self.training_metrics = []
        self.val_from_previous_run = self.core_context.train._get_last_validation()

        # Training configs
        self.local_training = local_training
        self.latest_checkpoint = latest_checkpoint
        self.average_training_metrics = average_training_metrics
        self.test_mode = test_mode
        self.searcher_metric_name = searcher_metric_name
        self.ckpt_policy = checkpoint_policy
        self.smaller_is_better = smaller_is_better
        self.global_batch_size = self.context.get_global_batch_size()

        if torch.cuda.is_available():
            self.prof._set_sync_device(self._sync_device)
        self.callbacks = self.trial.build_callbacks()
        for callback in self.callbacks.values():
            if util.is_overridden(callback.on_checkpoint_end, pytorch.PyTorchCallback):
                warnings.warn(
                    "The on_checkpoint_end callback is deprecated, please use "
                    "on_checkpoint_write_end instead.",
                    FutureWarning,
                )

        if len(self.context.models) == 0:
            raise det.errors.InvalidExperimentException(
                "Must have at least one model. "
                "This might be caused by not wrapping your model with wrap_model().",
            )
        if len(self.context.optimizers) == 0:
            raise det.errors.InvalidExperimentException(
                "Must have at least one optimizer. "
                "This might be caused by not wrapping your optimizer with wrap_optimizer().",
            )
        self._check_evaluate_implementation()

        # Currently only horovod and torch backends are supported for distributed training
        if self.context.distributed.size > 1:
            assert (
                self.use_horovod or self.use_torch
            ), "Must use horovod or torch for distributed training"
        if context.distributed.size > 1 and not self.is_chief:
            log_level = (
                logging.DEBUG if debug else logging.WARNING
            )
            logging.getLogger().setLevel(log_level)
        self.metric_writer = self.create_metric_writer()

    @staticmethod
    def from_env(trial_inst: "PyTorchTrial",
                 context: PyTorchTrialContext,
                 env: det.EnvContext,
                 ):
        return PyTorchTrialController(
            trial_inst=trial_inst,
            context=context,
            min_checkpoint_period=TrainUnit.from_values(**env.experiment_config.get_min_checkpoint_period()),
            min_validation_period=TrainUnit.from_values(**env.experiment_config.get_min_validation_period()),
            average_training_metrics=env.experiment_config.average_training_metrics_enabled(),
            checkpoint_policy=env.experiment_config.get_checkpoint_policy(),
            smaller_is_better=env.experiment_config.get_smaller_is_better(),
            searcher_metric_name=env.experiment_config.get_searcher_metric(),
            local_training=False,
            det_profiler=ProfilerAgent.from_env(env=env, global_rank=context.distributed.rank,
                                                local_rank=context.distributed.local_rank),
            steps_completed=env.steps_completed,
            debug=env.debug
        )

    @classmethod
    def create_metric_writer(
        cls: Type["PyTorchTrialController"],
    ) -> tensorboard.BatchMetricWriter:
        from determined.tensorboard.metric_writers.pytorch import TorchWriter

        writer = TorchWriter()
        return tensorboard.BatchMetricWriter(writer)

    @classmethod
    def pre_execute_hook(
        cls: Type["PyTorchTrialController"],
        trial_seed: int,
        distributed_backend: det._DistributedBackend,
    ) -> None:
        # Initialize the correct horovod.
        if distributed_backend.use_horovod():
            hvd.require_horovod_type("torch", "PyTorchTrial is in use.")
            hvd.init()
        if distributed_backend.use_torch():
            if torch.cuda.is_available():
                dist.init_process_group(backend="nccl")  # type: ignore
            else:
                dist.init_process_group(backend="gloo")  # type: ignore

        cls._set_random_seeds(trial_seed)

    def upload_tb_files(self) -> None:
        self.core_context.train.upload_tensorboard_files(
            (lambda _: True) if self.is_chief else (lambda p: not p.match("*tfevents*")),
            get_rank_aware_path,
        )

    @staticmethod
    def _set_random_seeds(seed: int) -> None:
        # Set identical random seeds on all training processes.
        # When using horovod, each worker will start at a unique
        # offset in the dataset, ensuring that it is processing a unique
        # training batch.
        random.seed(seed)
        np.random.seed(seed)
        torch.random.manual_seed(seed)
        # TODO(Aaron): Add flag to enable determinism.
        # torch.backends.cudnn.deterministic = True
        # torch.backends.cudnn.benchmark = False

    def _report_training_metrics(self):
        # It is possible we have already report training metrics for this step from checkpointing
        if not self.training_metrics:
            return

        # Aggregate and reduce training metrics from all the training processes.
        if self.context.distributed.size > 1 and self.average_training_metrics:
            with self.prof.record_timing("average_training_metrics"):
                batch_metrics = pytorch._combine_and_average_training_metrics(
                    self.context.distributed, self.training_metrics
                )
        else:
            batch_metrics = self.training_metrics

        metrics = det.util.make_metrics(None, batch_metrics)

        # Ignore batch_metrics entirely for custom reducers; there's no guarantee that per-batch
        # metrics are even logical for a custom reducer.
        with self.prof.record_timing("reduce_metrics"):
            metrics["avg_metrics"].update(
                pytorch._convert_metrics_to_numpy(self.context.reduce_metrics(for_training=True))
            )

        metrics = self.context.distributed.broadcast(metrics)

        for callback in self.callbacks.values():
            callback.on_training_workload_end(
                avg_metrics=metrics["avg_metrics"],
                batch_metrics=metrics["batch_metrics"],
            )

        # Only report on the chief worker
        if self.is_chief:
            avg_metrics = metrics.get("avg_metrics", {})
            batch_metrics = metrics.get("batch_metrics", [])

            self.metric_writer.on_train_step_end(
                self.state.batches_trained,
                avg_metrics,
                batch_metrics,
            )
            self.core_context.train.report_training_metrics(
                steps_completed=self.state.batches_trained, metrics=avg_metrics, batch_metrics=batch_metrics
            )

        self.context.reset_reducers()

        # Clear training metrics for next reporting step
        self.training_metrics = []

    def _report_validation_metrics(self, metrics):
        if not self.is_chief:
            return
        # Check if op complete
        searcher_metric = None
        if self.searcher_op:
            searcher_metric = self._validate_searcher_metric(metrics)
            if self._steps_remaining() < 1:
                self.searcher_op.report_completed(searcher_metric)

        self.core_context.train.report_validation_metrics(self.state.batches_trained, metrics)

        if self.state.last_ckpt != self.state.batches_trained:
            if self.ckpt_policy == "all" or (
                self.ckpt_policy == "best"
            ):
                self.checkpoint(already_exiting=False)
            elif self.ckpt_policy == "best" and searcher_metric:
                self.is_best_validation(now=searcher_metric, before=self.core_context.train.get_experiment_best_validation())
                self.checkpoint(already_exiting=False)

    def is_best_validation(self, now: float, before: Optional[float]) -> bool:
        if before is None:
            return True

        return (now < before) if self.smaller_is_better else (now > before)

    def on_epoch_start(self, epoch_idx: int):
        for callback in self.callbacks.values():
            with self.prof.record_timing(
                f"callbacks.{callback.__class__.__name__}.on_training_epoch_start"
            ):
                sig = signature(callback.on_training_epoch_start)
                if sig.parameters:
                    callback.on_training_epoch_start(epoch_idx)
                else:
                    logging.warning(
                        "on_training_epoch_start() without parameters is deprecated"
                        " since 0.17.8. Please add epoch_idx parameter."
                    )
                    callback.on_training_epoch_start()  # type: ignore[call-arg]

    def on_epoch_end(self, epoch_idx: int):
        for callback in self.callbacks.values():
            with self.prof.record_timing(
                f"callbacks.{callback.__class__.__name__}.on_training_epoch_end"
            ):
                callback.on_training_epoch_end(epoch_idx)

    def checkpoint(self, already_exiting: bool):
        if self.state.last_ckpt == self.state.batches_trained:
            return

        self.core_context.train.set_status("checkpointing")
        self.state.last_ckpt = self.state.batches_trained

        uuid = ""
        try:
            if self.is_chief:
                metadata = {
                    "determined_version": det.__version__,
                    "steps_completed": self.state.batches_trained,
                    "framework": f"torch-{torch.__version__}",
                    "format": "pickle",
                }
                with self.context._core.checkpoint.store_path(metadata) as (
                    path,
                    storage_id,
                ):
                    self._save(path)
                    uuid = storage_id
            uuid = self.context.distributed.broadcast(uuid)
            for callback in self.callbacks.values():
                callback.on_checkpoint_upload_end(uuid=uuid)
        except det.InvalidHP:
            self.core_context.train.report_early_exit(core.EarlyExitReason.INVALID_HP)
            if not already_exiting:
                raise ShouldExit(skip_exit_checkpoint=True)

    @classmethod
    def from_trial(
        cls: Type["PyTorchTrialController"], *args: Any, **kwargs: Any
    ) -> det.TrialController:
        return cls(*args, **kwargs)

    @classmethod
    def supports_mixed_precision(cls: Type["PyTorchTrialController"]) -> bool:
        return True

    def _check_evaluate_implementation(self) -> None:
        """
        Check if the user has implemented evaluate_batch
        or evaluate_full_dataset.
        """
        logging.debug(f"Evaluate_batch_defined: {self._evaluate_batch_defined()}.")
        logging.debug(f"Evaluate full dataset defined: {self._evaluate_full_dataset_defined()}.")
        if self._evaluate_batch_defined() == self._evaluate_full_dataset_defined():
            raise det.errors.InvalidExperimentException(
                "Please define exactly one of: `evaluate_batch()` or `evaluate_full_dataset()`. "
                "For most use cases `evaluate_batch()` is recommended because "
                "it can be parallelized across all devices.",
            )

    def _evaluate_batch_defined(self) -> bool:
        return util.is_overridden(self.trial.evaluate_batch, PyTorchTrial)

    def _evaluate_full_dataset_defined(self) -> bool:
        return util.is_overridden(self.trial.evaluate_full_dataset, PyTorchTrial)

    def _set_data_loaders(self) -> None:
        skip_batches = self.state.batches_trained

        num_replicas = self.context.distributed.size
        rank = self.context.distributed.rank

        train_data = self.trial.build_training_data_loader()
        if isinstance(train_data, pytorch.DataLoader):
            self.training_loader = train_data.get_data_loader(
                repeat=True, skip=skip_batches, num_replicas=num_replicas, rank=rank
            )
        else:
            # Non-determined DataLoader; ensure the user meant to do this.
            if not self.context.experimental._data_repro_checks_disabled:
                raise RuntimeError(
                    pytorch._dataset_repro_warning("build_training_data_loader", train_data)
                )
            self.training_loader = train_data

        # All workers use the chief's definition of epoch lengths, which is based on the training
        # loader's len. If this len does not exist, epoch durations cannot be deduced, and they
        # default to max int.
        try:
            epoch_len = len(self.training_loader)
        except TypeError:
            epoch_len = sys.maxsize
        self.context._epoch_len = self.context.distributed.broadcast(epoch_len)

        # Validation loader will be undefined on process ranks > 0
        # when the user defines `validate_full_dataset()`.
        self.validation_loader = None  # type: Optional[torch.utils.data.DataLoader]
        validation_data = self.trial.build_validation_data_loader()
        if self._evaluate_batch_defined():
            if isinstance(validation_data, pytorch.DataLoader):
                self.validation_loader = validation_data.get_data_loader(
                    repeat=False, skip=0, num_replicas=num_replicas, rank=rank
                )
            else:
                # Non-determined DataLoader; ensure the user meant to do this.
                if not self.context.experimental._data_repro_checks_disabled:
                    raise RuntimeError(
                        pytorch._dataset_repro_warning(
                            "build_validation_data_loader", validation_data
                        )
                    )
                self.validation_loader = validation_data
        elif self.is_chief:
            if isinstance(validation_data, pytorch.DataLoader):
                self.validation_loader = validation_data.get_data_loader(
                    repeat=False, skip=0, num_replicas=1, rank=0
                )
            else:
                # Non-determined DataLoader; ensure the user meant to do this.
                if not self.context.experimental._data_repro_checks_disabled:
                    raise RuntimeError(
                        pytorch._dataset_repro_warning(
                            "build_validation_data_loader", validation_data
                        )
                    )
                self.validation_loader = validation_data

    def _step_batch(self):
        self.state.batches_trained += 1
        self.state.epochs_trained = self.get_epoch_idx(self.state.batches_trained)
        # True epoch-based training is not supported. Epoch start/end is calculated with batch.
        if self.context.is_epoch_start():
            self.on_epoch_start(self.state.epochs_trained)

        if self.context.is_epoch_end():
            self.on_epoch_end(self.state.epochs_trained)

        self._train_step()

    def _should_validate(self) -> bool:
        if isinstance(self.min_validation_period, Batch):
            return self.state.batches_trained % max(self.min_validation_period.value, 1) == 0
        elif isinstance(self.min_validation_period, Epoch):
            return self.state.epochs_trained % max(self.min_validation_period.value, 1) == 0
        elif isinstance(self.min_validation_period, Record):
            return (self.state.batches_trained * self.global_batch_size) % max(self.min_validation_period.value, 1) == 0
        else:
            return False

    def _should_checkpoint(self) -> bool:
        if isinstance(self.min_checkpoint_period, Batch):
            return self.state.batches_trained % max(self.min_checkpoint_period.value, 1) == 0
        elif isinstance(self.min_checkpoint_period, Epoch):
            return self.state.epochs_trained % max(self.min_checkpoint_period.value, 1) == 0
        elif isinstance(self.min_checkpoint_period, Record):
            return (self.state.batches_trained * self.global_batch_size) % max(self.min_checkpoint_period.value, 1) == 0
        else:
            return False

    def _train_step(self):
        should_validate = self._should_validate()
        should_checkpoint = self._should_checkpoint()
        should_report = self.state.batches_trained % self.scheduling_unit == 0

        if not any([should_validate, should_checkpoint, should_report]):
            # No validation/checkpointing for this step, continue training
            return

        # Train step begin: report metrics and searcher before a validation or checkpoint step
        if self.searcher_op:
            self._report_searcher_progress(self.searcher_op)
        self._report_training_metrics()

        if should_validate:
            val_metrics = self._validate()
            self._report_validation_metrics(val_metrics)

        if should_checkpoint:
            self.checkpoint(already_exiting=False)

        # Train step end: check preemption and upload to tensorboard
        self.upload_tb_files()
        self.stop_requested()

    def stop_requested(self):
        if self.core_context.distributed.rank == 0:
            if self.core_context.preempt.should_preempt():
                raise ShouldExit()
        if self.context.get_stop_requested():
            raise ShouldExit()

    def _steps_remaining(self):
        if isinstance(self.max_length, Batch):
            return self.max_length.value - self.state.batches_trained
        elif isinstance(self.max_length, Epoch):
            return self.max_length.value - self.state.epochs_trained
        elif isinstance(self.max_length, Record):
            return self.max_length.value - (self.state.batches_trained * self.global_batch_size)

    def _train(self):
        for batch_idx, batch in enumerate(self.training_iterator):
            training_metrics = self._train_batch(self.state.batches_trained, batch,
                                                 self.state.epochs_trained, batch_idx)
            self.training_metrics.append(training_metrics)
            self._step_batch()
            if self._steps_remaining() == 0:
                return

    def _report_searcher_progress(self, op: core.SearcherOperation):
        if op._completed:
            return

        if self.is_chief:
            if self._searcher_unit == core.Unit.BATCHES:
                op.report_progress(self.state.batches_trained)
            elif self._searcher_unit == core.Unit.RECORDS:
                op.report_progress(self.context.get_global_batch_size() * self.state.batches_trained)
            elif self._searcher_unit == core.Unit.EPOCHS:
                op.report_progress(self.state.epochs_trained)
            else:
                raise ValueError(f"unrecognized searcher op unit: {self._searcher_unit}")

    def run(self):
        @contextlib.contextmanager
        def defer(fn: Callable, *args: Any) -> Iterator[None]:
            try:
                yield
            finally:
                fn(*args)

        # We define on_shutdown here instead of inside the `for callback in...` loop to ensure we
        # don't bind the loop iteration variable `callback`, which would likely cause us to call
        # on_trial_shutdown() multiple times for the final callback, and not at all for the others.
        def on_shutdown(callback_name: str, on_trial_shutdown: Callable) -> None:
            with self.prof.record_timing(f"callbacks.{callback_name}.on_trial_shutdown"):
                on_trial_shutdown()

        with contextlib.ExitStack() as exit_stack:
            for callback in self.callbacks.values():
                with self.prof.record_timing(
                    f"callbacks.{callback.__class__.__name__}.on_trial_startup"
                ):
                    callback.on_trial_startup(self.state.batches_trained, self.latest_checkpoint)
                exit_stack.enter_context(
                    defer(on_shutdown, callback.__class__.__name__, callback.on_trial_shutdown)
                )

            self._set_data_loaders()

            # We create the training_iterator here rather than in __init__ because we have to be
            # careful to trigger its shutdown explicitly, to avoid hangs in when the user is using
            # multiprocessing-based parallelism for their dataloader.
            #
            # We create it before loading state because we don't want the training_iterator
            # shuffling values after we load state.
            self.training_iterator = iter(self.training_loader)

            def cleanup_iterator() -> None:
                # Explicitly trigger the training iterator's shutdown (which happens in __del__).
                # See the rather long note in pytorch/torch/utils/data/dataloader.py.
                del self.training_iterator

            exit_stack.enter_context(defer(cleanup_iterator))

            # If a load path is provided load weights and restore the data location.
            if self.latest_checkpoint is not None:
                logging.info(f"Restoring trial from checkpoint {self.latest_checkpoint}")
                with self.context._core.checkpoint.restore_path(
                    self.latest_checkpoint
                ) as load_path:
                    self._load(load_path)

            if self.context.distributed.size > 1 and self.use_horovod:
                hvd.broadcast_parameters(self.context._main_model.state_dict(), root_rank=0)
                for optimizer in self.context.optimizers:
                    hvd.broadcast_optimizer_state(optimizer, root_rank=0)

            exit_stack.enter_context(self.prof)
            for callback in self.callbacks.values():
                with self.prof.record_timing(
                    f"callbacks.{callback.__class__.__name__}.on_training_start"
                ):
                    callback.on_training_start()
            if self.local_training:
                self._train_for_local()
                return

            for op in self.core_context.searcher.operations():
                self._train_for_op(op)
                if self.is_chief:
                    assert op._completed, "logic error; op was never completed"

    def _train_for_local(self):
        self._train()

        logging.debug(f"Training finished after {self.max_length.value} steps.")

        # Report metrics after searcher complete if there are remaining unreported
        self._report_training_metrics()

        # Validate and report validation metrics if last validation step is not current step
        if self.state.last_val != self.state.batches_trained:
            val_metrics = self._validate()
            self._report_validation_metrics(val_metrics)

        # Checkpoint if last checkpoint is not current
        if self.state.last_ckpt != self.state.batches_trained:
            self.checkpoint(already_exiting=False)

    def _train_for_op(self, op: SearcherOperation):
        self.searcher_op = op
        self.max_length = TrainUnit.from_searcher_unit(op.length, self._searcher_unit)
        self._train()

        logging.debug(f"Training finished after {op.length} steps.")

        # Report metrics after searcher complete if there are remaining unreported
        self._report_training_metrics()

        # Validate and report validation metrics if last validation step is not current step
        if self.state.last_val != self.state.batches_trained:
            val_metrics = self._validate()
            self._report_validation_metrics(val_metrics)

        # Checkpoint if last checkpoint is not current
        if self.state.last_ckpt != self.state.batches_trained:
            self.checkpoint(already_exiting=False)

    def _validate_searcher_metric(self, val_metrics):
        if self.searcher_metric_name not in val_metrics:
            raise RuntimeError(
                f"Search method is configured to use metric '{self.searcher_metric_name}' but model "
                f"definition returned validation metrics {list(val_metrics.keys())}. The metric "
                "used by the search method must be one of the validation "
                "metrics returned by the model definition."
            )

        # Check that the searcher metric has a scalar value so that it can be compared for
        # search purposes. Other metrics don't have to be scalars.
        searcher_metric = val_metrics[self.searcher_metric_name]
        if not tensorboard.metric_writers.util.is_numerical_scalar(searcher_metric):
            raise RuntimeError(
                f"Searcher validation metric '{self.searcher_metric_name}' returned "
                f"a non-scalar value: {searcher_metric}"
            )
        return searcher_metric

    def get_epoch_idx(self, batch_id: int) -> int:
        return batch_id // self.context._epoch_len  # type: ignore

    def _auto_step_lr_scheduler_per_batch(
        self, batch_idx: int, lr_scheduler: pytorch.LRScheduler
    ) -> None:
        """
        This function automatically steps an LR scheduler. It should be called per batch.
        """
        # Never step lr when we do not step optimizer.
        if not self.context._should_communicate_and_update():
            return

        if lr_scheduler._step_mode == pytorch.LRScheduler.StepMode.STEP_EVERY_BATCH:
            start_idx = batch_idx - self.context._aggregation_frequency + 1
            for i in range(start_idx, batch_idx + 1):
                if (i + 1) % lr_scheduler._frequency == 0:
                    lr_scheduler.step()
        elif lr_scheduler._step_mode == pytorch.LRScheduler.StepMode.STEP_EVERY_OPTIMIZER_STEP:
            if (batch_idx + 1) % lr_scheduler._frequency == 0:
                lr_scheduler.step()
        elif lr_scheduler._step_mode == pytorch.LRScheduler.StepMode.STEP_EVERY_EPOCH:
            # We will step if the next optimizer step will land in the next epoch.
            epoch_idx = self.get_epoch_idx(batch_idx)
            next_steppable_batch = batch_idx + self.context._aggregation_frequency
            next_batch_epoch_idx = self.get_epoch_idx(next_steppable_batch)
            for e in range(epoch_idx, next_batch_epoch_idx):
                if (e + 1) % lr_scheduler._frequency == 0:
                    lr_scheduler.step()

    def _should_update_scaler(self) -> bool:
        if not self.context._scaler or not self.context.experimental._auto_amp:
            return False
        return self.context._should_communicate_and_update()  # type: ignore

    def _train_batch(self, batches_trained, batch, epoch_idx, batch_idx):
        # Set the batch index on the trial context used by step_optimizer
        self.context._current_batch_idx = batches_trained

        # Initialize profiler
        batch_start_time = time.time()
        self.prof.update_batch_idx(batch_idx)

        if self.context.experimental._auto_to_device:
            with self.prof.record_timing("to_device", accumulate=True):
                batch = self.context.to_device(batch)

        with contextlib.ExitStack() as exit_stack:
            exit_stack.enter_context(self.prof.record_timing("train_batch", requires_sync=False))
            if self.context.profiler:
                exit_stack.enter_context(self.context.profiler)

            training_metrics = self.trial.train_batch(
                batch=batch,
                epoch_idx=epoch_idx,
                batch_idx=batch_idx,
            )

            if self.context.profiler:
                self.context.profiler.step()

        if self._should_update_scaler():
            # We update the scaler once after train_batch is done because the GradScaler is
            # expected to be one-per-training-loop, with one .update() call after all .step(opt)
            # calls for that batch are completed [1].
            #
            # [1] pytorch.org/docs/master/notes/amp_examples.html
            #         #working-with-multiple-models-losses-and-optimizers
            self.context._scaler.update()

        if isinstance(training_metrics, torch.Tensor):
            training_metrics = {"loss": training_metrics}

        # Step learning rate of a pytorch.LRScheduler.
        with self.prof.record_timing("step_lr_schedulers"):
            for lr_scheduler in self.context.lr_schedulers:
                self._auto_step_lr_scheduler_per_batch(batch_idx, lr_scheduler)

        with self.prof.record_timing("from_device"):
            for name, metric in training_metrics.items():
                # Convert PyTorch metric values to NumPy, so that
                # `det.util.encode_json` handles them properly without
                # needing a dependency on PyTorch.
                if isinstance(metric, torch.Tensor):
                    metric = metric.cpu().detach().numpy()
                training_metrics[name] = metric

        batch_dur = time.time() - batch_start_time
        samples_per_second = self.trial.get_batch_length(batch) / batch_dur
        samples_per_second *= self.context.distributed.size
        self.prof.record_metric("samples_per_second", samples_per_second)

        check.is_instance(
            training_metrics,
            dict,
            "train_batch() must return a dictionary "
            f"mapping string names to Tensor metrics, got {type(training_metrics)}",
        )

        return training_metrics

    @torch.no_grad()  # type: ignore
    def _validate(self):
        if self.state.last_val == self.state.batches_trained:
            return

        # Report a validation step is starting.
        if self.is_chief:
            self.core_context.train.set_status("validating")

        self.context.reset_reducers()

        # Set the behavior of certain layers (e.g., dropout) that are
        # different between training and inference.
        for model in self.context.models:
            model.eval()

        step_start_time = time.time()

        for callback in self.callbacks.values():
            if util.is_overridden(callback.on_validation_step_start, pytorch.PyTorchCallback):
                logging.warning(
                    "on_validation_step_start is now deprecated, "
                    "please use on_validation_start instead"
                )
                callback.on_validation_step_start()

        for callback in self.callbacks.values():
            callback.on_validation_start()

        num_inputs = 0
        metrics = {}  # type: Dict[str, Any]

        if self._evaluate_batch_defined():
            keys = None
            batch_metrics = []

            assert isinstance(self.validation_loader, torch.utils.data.DataLoader)
            if len(self.validation_loader) == 0:
                raise RuntimeError("validation_loader is empty.")
            for callback in self.callbacks.values():
                callback.on_validation_epoch_start()
            for idx, batch in enumerate(self.validation_loader):
                if self.context.experimental._auto_to_device:
                    with self.prof.record_timing("to_device", accumulate=True):
                        batch = self.context.to_device(batch)
                num_inputs += self.trial.get_batch_length(batch)

                if has_param(self.trial.evaluate_batch, "batch_idx", 2):
                    vld_metrics = self.trial.evaluate_batch(batch=batch, batch_idx=idx)
                else:
                    vld_metrics = self.trial.evaluate_batch(batch=batch)  # type: ignore
                # Verify validation metric names are the same across batches.
                if keys is None:
                    keys = vld_metrics.keys()
                else:
                    if keys != vld_metrics.keys():
                        raise ValueError(
                            "Validation metric names must match across all batches of data: "
                            f"{keys} != {vld_metrics.keys()}.",
                        )
                if not isinstance(vld_metrics, dict):
                    raise TypeError(
                        "validation_metrics() must return a "
                        "dictionary of string names to Tensor "
                        "metrics; "
                        f"got {vld_metrics}.",
                    )
                # TODO: For performance perform -> cpu() only at the end of validation.
                batch_metrics.append(pytorch._convert_metrics_to_numpy(vld_metrics))
                if self.test_mode:
                    break

            for callback in self.callbacks.values():
                callback.on_validation_epoch_end(batch_metrics)

            metrics = pytorch._reduce_metrics(
                self.context.distributed,
                batch_metrics=batch_metrics,
                keys=keys,
                metrics_reducers=pytorch._prepare_metrics_reducers(
                    self.trial.evaluation_reducer(), keys=keys
                ),
            )

            # Gather a list of per-worker (num_inputs, num_batches) tuples.
            input_counts = self.context.distributed.gather((num_inputs, idx + 1))
            if self.context.distributed.rank == 0:
                assert input_counts is not None
                # Reshape and sum.
                num_inputs, num_batches = [sum(n) for n in zip(*input_counts)]

        else:
            assert self._evaluate_full_dataset_defined(), "evaluate_full_dataset not defined."
            self.validation_loader = cast(torch.utils.data.DataLoader, self.validation_loader)
            if self.is_chief:
                metrics = self.trial.evaluate_full_dataset(data_loader=self.validation_loader)

                if not isinstance(metrics, dict):
                    raise TypeError(
                        f"eval() must return a dictionary, got {type(metrics).__name__}."
                    )

                metrics = pytorch._convert_metrics_to_numpy(metrics)
                num_inputs = self.context.get_per_slot_batch_size() * len(self.validation_loader)

        metrics.update(
            pytorch._convert_metrics_to_numpy(self.context.reduce_metrics(for_training=False))
        )

        if self.context.distributed.size > 1 and any(
            util.is_overridden(c.on_validation_end, pytorch.PyTorchCallback)
            or util.is_overridden(c.on_validation_step_end, pytorch.PyTorchCallback)
            for c in self.callbacks.values()
        ):
            logging.debug(
                "Broadcasting metrics to all worker processes to execute a "
                "validation step end callback"
            )
            metrics = self.context.distributed.broadcast(metrics)

        for callback in self.callbacks.values():
            if util.is_overridden(callback.on_validation_step_end, pytorch.PyTorchCallback):
                logging.warning(
                    "on_validation_step_end is now deprecated, please use on_validation_end instead"
                )
                callback.on_validation_step_end(metrics)

        for callback in self.callbacks.values():
            callback.on_validation_end(metrics)

        self.state.last_val = self.state.batches_trained

        if self.is_chief:
            # Skip reporting timings if evaluate_full_dataset() was defined.  This is far less common
            # than evaluate_batch() and we can't know how the user processed their validation data.
            if self._evaluate_batch_defined():
                step_duration = time.time() - step_start_time
                logging.info(
                    det.util.make_timing_log("validated", step_duration, num_inputs, num_batches)
                )
            self.metric_writer.on_validation_step_end(self.state.batches_trained, metrics)


        # Set models back to train mode
        for model in self.context.models:
            model.train()
        self.core_context.train.set_status("training")
        
        if self.is_chief:
            return metrics
        else:
            return {}

    def _load(self, load_path: pathlib.Path) -> None:
        # Backwards compat with older checkpoint formats. List is of the newest to
        # the oldest known state_dict locations.
        potential_paths = [
            ["state_dict.pth"],
            ["determined", "state_dict.pth"],
            ["pedl", "state_dict.pth"],
            ["checkpoint.pt"],
        ]

        checkpoint: Optional[Dict[str, Any]] = None
        for ckpt_path in potential_paths:
            maybe_ckpt = load_path.joinpath(*ckpt_path)
            if maybe_ckpt.exists():
                checkpoint = torch.load(str(maybe_ckpt), map_location="cpu")  # type: ignore
                break
        if checkpoint is None or not isinstance(checkpoint, dict):
            return

        for callback in self.callbacks.values():
            callback.on_checkpoint_load_start(checkpoint)

        if "model_state_dict" in checkpoint:
            # Backward compatible with older checkpoint format.
            if "models_state_dict" in checkpoint:
                raise RuntimeError("Both model_state_dict and models_state_dict in checkpoint.")
            if len(self.context.models) > 1:
                raise RuntimeError(
                    "Old-format checkpoint cannot be loaded into a context with more than one "
                    "model."
                )
            self.context.models[0].load_state_dict(checkpoint["model_state_dict"])
        else:
            for idx, model in enumerate(self.context.models):
                model_state_dict = checkpoint["models_state_dict"][idx]
                try:
                    model.load_state_dict(model_state_dict)
                except Exception:
                    # If the checkpointed model is non-DDP and the current model is DDP, append
                    # module prefix to the checkpointed data
                    if isinstance(model, torch.nn.parallel.DistributedDataParallel):
                        logging.debug("Loading non-DDP checkpoint into a DDP model")
                        self._add_prefix_in_state_dict_if_not_present(model_state_dict, "module.")
                    else:
                        # If the checkpointed model is DDP and if we are currently running in
                        # single-slot mode, remove the module prefix from checkpointed data
                        logging.debug("Loading DDP checkpoint into a non-DDP model")
                        torch.nn.modules.utils.consume_prefix_in_state_dict_if_present(
                            model_state_dict, "module."
                        )
                    model.load_state_dict(model_state_dict)

        if "optimizer_state_dict" in checkpoint:
            # Backward compatible with older checkpoint format.
            if "optimizers_state_dict" in checkpoint:
                raise RuntimeError(
                    "Both optimizer_state_dict and optimizers_state_dict in checkpoint."
                )
            if len(self.context.optimizers) > 1:
                raise RuntimeError(
                    "Old-format checkpoint cannot be loaded into a context with more than one "
                    "optimizer."
                )
            self.context.optimizers[0].load_state_dict(checkpoint["optimizer_state_dict"])
        else:
            for idx, optimizer in enumerate(self.context.optimizers):
                optimizer.load_state_dict(checkpoint["optimizers_state_dict"][idx])

        if "lr_scheduler" in checkpoint:
            # Backward compatible with older checkpoint format.
            if "lr_schedulers_state_dict" in checkpoint:
                raise RuntimeError("Both lr_scheduler and lr_schedulers_state_dict in checkpoint.")
            if len(self.context.lr_schedulers) > 1:
                raise RuntimeError(
                    "Old-format checkpoint cannot be loaded into a context with more than one LR "
                    "scheduler."
                )
            self.context.lr_schedulers[0].load_state_dict(checkpoint["lr_scheduler"])
        else:
            for idx, lr_scheduler in enumerate(self.context.lr_schedulers):
                lr_scheduler.load_state_dict(checkpoint["lr_schedulers_state_dict"][idx])

        if "scaler_state_dict" in checkpoint:
            if self.context._scaler:
                self.context._scaler.load_state_dict(checkpoint["scaler_state_dict"])
            else:
                logging.warning(
                    "There exists scaler_state_dict in checkpoint but the experiment is not using "
                    "AMP."
                )
        else:
            if self.context._scaler:
                logging.warning(
                    "The experiment is using AMP but scaler_state_dict does not exist in the "
                    "checkpoint."
                )

        if "amp_state" in checkpoint:
            if self.context._use_apex:
                apex.amp.load_state_dict(checkpoint["amp_state"])
            else:
                logging.warning(
                    "There exists amp_state in checkpoint but the experiment is not using Apex."
                )
        else:
            if self.context._use_apex:
                logging.warning(
                    "The experiment is using Apex but amp_state does not exist in the checkpoint."
                )

        if "rng_state" in checkpoint:
            rng_state = checkpoint["rng_state"]
            np.random.set_state(rng_state["np_rng_state"])
            random.setstate(rng_state["random_rng_state"])
            torch.random.set_rng_state(rng_state["cpu_rng_state"])

            if torch.cuda.device_count():
                if "gpu_rng_state" in rng_state:
                    torch.cuda.set_rng_state(
                        rng_state["gpu_rng_state"], device=self.context.distributed.local_rank
                    )
                else:
                    logging.warning(
                        "The system has a gpu but no gpu_rng_state exists in the checkpoint."
                    )
            else:
                if "gpu_rng_state" in rng_state:
                    logging.warning(
                        "There exists gpu_rng_state in checkpoint but the system has no gpu."
                    )
        else:
            logging.warning("The checkpoint has no random state to restore.")

        callback_state = checkpoint.get("callbacks", {})
        for name in self.callbacks:
            if name in callback_state:
                self.callbacks[name].load_state_dict(callback_state[name])
            elif util.is_overridden(self.callbacks[name].load_state_dict, pytorch.PyTorchCallback):
                logging.warning(
                    f"Callback '{name}' implements load_state_dict(), but no callback state "
                    "was found for that name when restoring from checkpoint. This "
                    "callback will be initialized from scratch"
                )

        save_path = load_path.joinpath("trial_state.pkl")
        if save_path.exists():
            with save_path.open("rb") as f:
                self.load_state(pickle.load(f))
        else:
            # Support legacy save states
            wlsq_path = load_path.joinpath("workload_sequencer.pkl")
            if wlsq_path.exists():
                with wlsq_path.open("rb") as f:
                    self.load_state(pickle.load(f))

    def load_state(self, state: Any) -> None:
        # Load our state from the checkpoint if we are continuing training after a pause or restart.
        # If the trial_id doesn't match our current trial id, we're continuing training a previous
        # trial and the state in the checkpoint should be discarded.
        if state.get("trial_id") != self._trial_id:
            return

        self.state = TrialState(**state)

        # Detect the case where the final validation we made was against this exact checkpoint.  In
        # that case, the master will know about the validation, but it would not appear in the
        # checkpoint state.  If the validation was before the last checkpoint, the checkpoint state
        # is already correct, while any validations after the last checkpoint aren't valid anymore
        # and can be safely ignored.
        if self.state.batches_trained == self.val_from_previous_run:
            self.state.last_val = self.state.batches_trained

    def _save(self, path: pathlib.Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

        util.write_user_code(path, not self.local_training)

        rng_state = {
            "cpu_rng_state": torch.random.get_rng_state(),
            "np_rng_state": np.random.get_state(),
            "random_rng_state": random.getstate(),
        }

        if torch.cuda.device_count():
            rng_state["gpu_rng_state"] = torch.cuda.get_rng_state(
                self.context.distributed.local_rank
            )

        # PyTorch uses optimizer objects that take the model parameters to
        # optimize on construction, so we store and reload the `state_dict()`
        # of the model and optimizer explicitly (instead of dumping the entire
        # objects) to avoid breaking the connection between the model and the
        # optimizer.
        checkpoint = {
            "models_state_dict": [model.state_dict() for model in self.context.models],
            "optimizers_state_dict": [
                optimizer.state_dict() for optimizer in self.context.optimizers
            ],
            "lr_schedulers_state_dict": [
                lr_scheduler.state_dict() for lr_scheduler in self.context.lr_schedulers
            ],
            "callbacks": {name: callback.state_dict() for name, callback in self.callbacks.items()},
            "rng_state": rng_state,
        }

        if self.context._scaler:
            checkpoint["scaler_state_dict"] = self.context._scaler.state_dict()

        if self.context._use_apex:
            checkpoint["amp_state"] = apex.amp.state_dict()

        for callback in self.callbacks.values():
            callback.on_checkpoint_save_start(checkpoint)

        torch.save(checkpoint, str(path.joinpath("state_dict.pth")))

        with path.joinpath("trial_state.pkl").open("wb") as f:
            pickle.dump(vars(self.state), f)

        trial_cls = type(self.trial)
        with open(path.joinpath("load_data.json"), "w") as f2:
            json.dump(
                {
                    "trial_type": "PyTorchTrial",
                    "experiment_config": self.context.get_experiment_config(),
                    "hparams": self.context.get_hparams(),
                    "trial_cls_spec": f"{trial_cls.__module__}:{trial_cls.__qualname__}",
                },
                f2,
            )

        for callback in self.callbacks.values():
            # TODO(DET-7912): remove on_checkpoint_end once it has been deprecated long enough.
            callback.on_checkpoint_end(str(path))
            callback.on_checkpoint_write_end(str(path))

    def _sync_device(self) -> None:
        torch.cuda.synchronize(self.context.device)

    @staticmethod
    def _add_prefix_in_state_dict_if_not_present(state_dict: Dict[str, Any], prefix: str) -> None:
        """Adds the prefix in state_dict in place, if it does not exist.
        ..note::
            Given a `state_dict` from a non-DDP model, a DDP model can load it by applying
            `_add_prefix_in_state_dict_if_present(state_dict, "module.")` before calling
            :meth:`torch.nn.Module.load_state_dict`.
        Args:
            state_dict (OrderedDict): a state-dict to be loaded to the model.
            prefix (str): prefix.
        """
        keys = sorted(state_dict.keys())
        for key in keys:
            if not key.startswith(prefix):
                newkey = prefix + key
                state_dict[newkey] = state_dict.pop(key)

        # also add the prefix to metadata if not exists.
        if "_metadata" in state_dict:
            metadata = state_dict["_metadata"]
            for key in list(metadata.keys()):
                if not key.startswith(prefix):
                    newkey = prefix + key
                    metadata[newkey] = metadata.pop(key)


class PyTorchTrial(det.Trial):
    """
    PyTorch trials are created by subclassing this abstract class.

    We can do the following things in this trial class:

    * **Define models, optimizers, and LR schedulers**.

       In the :meth:`__init__` method, initialize models, optimizers, and LR schedulers
       and wrap them with ``wrap_model``, ``wrap_optimizer``, ``wrap_lr_scheduler``
       provided by :class:`~determined.pytorch.PyTorchTrialContext`.

    * **Run forward and backward passes**.

       In :meth:`train_batch`, call ``backward`` and ``step_optimizer`` provided by
       :class:`~determined.pytorch.PyTorchTrialContext`.
       We support arbitrary numbers of models, optimizers, and LR schedulers
       and arbitrary orders of running forward and backward passes.

    * **Configure automatic mixed precision**.

       In the :meth:`__init__` method, call ``configure_apex_amp`` provided by
       :class:`~determined.pytorch.PyTorchTrialContext`.

    * **Clip gradients**.

       In :meth:`train_batch`, pass a function into
       ``step_optimizer(optimizer, clip_grads=...)`` provided by
       :class:`~determined.pytorch.PyTorchTrialContext`.
    """

    trial_controller_class = PyTorchTrialController
    trial_context_class = pytorch.PyTorchTrialContext

    @abstractmethod
    def __init__(self, context: pytorch.PyTorchTrialContext) -> None:
        """
        Initializes a trial using the provided ``context``. The general steps are:

        #. Initialize model(s) and wrap them with ``context.wrap_model``.
        #. Initialize optimizer(s) and wrap them with ``context.wrap_optimizer``.
        #. Initialize learning rate schedulers and wrap them with ``context.wrap_lr_scheduler``.
        #. If desired, wrap models and optimizer with ``context.configure_apex_amp``
           to use ``apex.amp`` for automatic mixed precision.
        #. Define custom loss function and metric functions.

        .. warning::

           You may see metrics for trials that are paused and later continued that are significantly
           different from trials that are not paused if some of your models, optimizers, and
           learning rate schedulers are not wrapped. The reason is that the model's state may not be
           restored accurately or completely from the checkpoint, which is saved to a checkpoint and
           then later loaded into the trial during resumed training. When using PyTorch, this can
           sometimes happen if the PyTorch API is not used correctly.

        Here is a code example.

        .. code-block:: python

            self.context = context

            self.a = self.context.wrap_model(MyModelA())
            self.b = self.context.wrap_model(MyModelB())
            self.opt1 = self.context.wrap_optimizer(torch.optm.Adam(self.a))
            self.opt2 = self.context.wrap_optimizer(torch.optm.Adam(self.b))

            (self.a, self.b), (self.opt1, self.opt2) = self.context.configure_apex_amp(
                models=[self.a, self.b],
                optimizers=[self.opt1, self.opt2],
                num_losses=2,
            )

            self.lrs1 = self.context.wrap_lr_scheduler(
                lr_scheduler=LambdaLR(self.opt1, lr_lambda=lambda epoch: 0.95 ** epoch),
                step_mode=LRScheduler.StepMode.STEP_EVERY_EPOCH,
            ))
        """
        pass

    @abstractmethod
    def train_batch(
        self, batch: pytorch.TorchData, epoch_idx: int, batch_idx: int
    ) -> Union[torch.Tensor, Dict[str, Any]]:
        """
        Train on one batch.

        Users should implement this function by doing the following things:

        1. Run forward passes on the models.

        2. Calculate the gradients with the losses with ``context.backward``.

        3. Call an optimization step for the optimizers with ``context.step_optimizer``.
           You can clip gradients by specifying the argument ``clip_grads``.

        4. Step LR schedulers if using manual step mode.

        5. Return training metrics in a dictionary.

        Here is a code example.

        .. code-block:: python

            # Assume two models, two optimizers, and two LR schedulers were initialized
            # in ``__init__``.

            # Calculate the losses using the models.
            loss1 = self.model1(batch)
            loss2 = self.model2(batch)

            # Run backward passes on losses and step optimizers. These can happen
            # in arbitrary orders.
            self.context.backward(loss1)
            self.context.backward(loss2)
            self.context.step_optimizer(
                self.opt1,
                clip_grads=lambda params: torch.nn.utils.clip_grad_norm_(params, 0.0001),
            )
            self.context.step_optimizer(self.opt2)

            # Step the learning rate.
            self.lrs1.step()
            self.lrs2.step()

            return {"loss1": loss1, "loss2": loss2}

        Arguments:
            batch (Dict[str, torch.Tensor], Sequence[torch.Tensor], torch.Tensor):
                batch of data for training.
            epoch_idx (integer): index of the current epoch among all the batches processed
                per device (slot) since the start of training.
            batch_idx (integer): index of the current batch among all the epochs processed
                per device (slot) since the start of training.
        Returns:
            torch.Tensor or Dict[str, Any]:
                training metrics to return.
        """
        pass

    @abstractmethod
    def build_training_data_loader(self) -> pytorch.DataLoader:
        """
        Defines the data loader to use during training.

        Must return an instance of :py:class:`determined.pytorch.DataLoader`.
        """
        pass

    @abstractmethod
    def build_validation_data_loader(self) -> pytorch.DataLoader:
        """
        Defines the data loader to use during validation.

        Must return an instance of :py:class:`determined.pytorch.DataLoader`.
        """
        pass

    def build_callbacks(self) -> Dict[str, pytorch.PyTorchCallback]:
        """
        Defines a dictionary of string names to callbacks to be used during
        training and/or validation.

        The string name will be used as the key to save and restore callback
        state for any callback that defines :meth:`load_state_dict` and :meth:`state_dict`.
        """
        return {}

    def evaluate_batch(self, batch: pytorch.TorchData, batch_idx: int) -> Dict[str, Any]:
        """
        Calculate validation metrics for a batch and return them as a
        dictionary mapping metric names to metric values. Per-batch validation metrics
        are reduced (aggregated) to produce a single set of validation metrics for the
        entire validation set (see :meth:`evaluation_reducer`).

        There are two ways to specify evaluation metrics. Either override
        :meth:`evaluate_batch` or :meth:`evaluate_full_dataset`. While
        :meth:`evaluate_full_dataset` is more flexible,
        :meth:`evaluate_batch` should be preferred, since it can be
        parallelized in distributed environments, whereas
        :meth:`evaluate_full_dataset` cannot. Only one of
        :meth:`evaluate_full_dataset` and :meth:`evaluate_batch` should be
        overridden by a trial.

        The metrics returned from this function must be JSON-serializable.

        Arguments:
            batch (Dict[str, torch.Tensor], Sequence[torch.Tensor], torch.Tensor):
                batch of data for evaluating.
            batch_idx (integer): index of the current batch among all the epochs processed
                per device (slot) since the start of training.
        """
        pass

    def predict_batch(self, batch: pytorch.TorchData) -> Dict[str, Any]:
        pass


    def evaluation_reducer(self) -> Union[pytorch.Reducer, Dict[str, pytorch.Reducer]]:
        """
        Return a reducer for all evaluation metrics, or a dict mapping metric
        names to individual reducers. Defaults to :obj:`determined.pytorch.Reducer.AVG`.
        """
        return pytorch.Reducer.AVG

    def evaluate_full_dataset(self, data_loader: torch.utils.data.DataLoader) -> Dict[str, Any]:
        """
        Calculate validation metrics on the entire validation dataset and
        return them as a dictionary mapping metric names to reduced metric
        values (i.e., each returned metric is the average or sum of that metric
        across the entire validation set).

        This validation cannot be distributed and is performed on a single
        device, even when multiple devices (slots) are used for training. Only
        one of :meth:`evaluate_full_dataset` and :meth:`evaluate_batch` should
        be overridden by a trial.

        The metrics returned from this function must be JSON-serializable.

        Arguments:
            data_loader (torch.utils.data.DataLoader): data loader for evaluating.
        """
        pass

    def get_batch_length(self, batch: Any) -> int:
        """Count the number of records in a given batch.

        Override this method when you are using custom batch types, as produced
        when iterating over the `DataLoader`.
        For example, when using `pytorch_geometric`:

        .. code-block:: python

            # Extra imports:
            from determined.pytorch import DataLoader
            from torch_geometric.data.dataloader import Collater

            # Trial methods:
            def build_training_data_loader(self):
                return DataLoader(
                    self.train_subset,
                    batch_size=self.context.get_per_slot_batch_size(),
                    collate_fn=Collater([], []),
                )

            def get_batch_length(self, batch):
                # `batch` is `torch_geometric.data.batch.Batch`.
                return batch.num_graphs

        Arguments:
            batch (Any): input training or validation data batch object.
        """
        return pytorch.data_length(batch)
