#!/usr/bin/env python
# coding: utf-8


from __future__ import absolute_import, division, print_function

import json
import logging
import math
import os
import random
import statistics
import warnings
from collections import defaultdict
from dataclasses import asdict
from itertools import chain
from multiprocessing import cpu_count

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from scipy.stats import mode, pearsonr
from sklearn.metrics import (
    confusion_matrix,
    f1_score,
    label_ranking_average_precision_score,
    matthews_corrcoef,
    mean_squared_error,
)
from torch.utils.tensorboard import SummaryWriter
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler, TensorDataset
from torch.utils.data.distributed import DistributedSampler
from tqdm.auto import tqdm, trange
from transformers.optimization import (
    get_constant_schedule,
    get_constant_schedule_with_warmup,
    get_linear_schedule_with_warmup,
    get_cosine_schedule_with_warmup,
    get_cosine_with_hard_restarts_schedule_with_warmup,
    get_polynomial_decay_schedule_with_warmup,
)
from transformers.optimization import AdamW, Adafactor
from transformers import (
    WEIGHTS_NAME,
    GPT2Config,
    GPT2DoubleHeadsModel,
    GPT2Tokenizer,
    OpenAIGPTConfig,
    OpenAIGPTDoubleHeadsModel,
    OpenAIGPTTokenizer,
    BlenderbotSmallTokenizer,
    BlenderbotForConditionalGeneration,
    BlenderbotTokenizer,
    BlenderbotConfig,
)

from simpletransformers.classification.classification_utils import (
    InputExample,
    convert_examples_to_features,
)
from simpletransformers.config.global_args import global_args
from simpletransformers.config.model_args import ConvAIArgs
from simpletransformers.config.utils import sweep_config_to_sweep_values
from simpletransformers.conv_ai.conv_ai_utils import get_dataset

try:
    import wandb

    wandb_available = True
except ImportError:
    wandb_available = False

logger = logging.getLogger(__name__)

SPECIAL_TOKENS = ["<bos>", "<eos>", "<speaker1>", "<speaker2>", "<pad>"]
ATTR_TO_SPECIAL_TOKEN = {
    "bos_token": "<bos>",
    "eos_token": "<eos>",
    "pad_token": "<pad>",
    "additional_special_tokens": ["<speaker1>", "<speaker2>"],
}
MODEL_INPUTS = ["input_ids", "mc_token_ids", "labels", "mc_labels", "token_type_ids"]
PADDED_INPUTS = ["input_ids", "labels", "token_type_ids"]


class ConvAIModel:
    def __init__(
        self,
        model_type,
        model_name,
        args=None,
        use_cuda=True,
        cuda_device=-1,
        **kwargs,
    ):

        """
        Initializes a ClassificationModel model.

        Args:
            model_type: The type of model (gpt, gpt2)
            model_name: Default Transformer model name or path to a directory containing Transformer model file (pytorch_nodel.bin).
            args (optional): Default args will be used if this parameter is not provided. If provided, it should be a dict containing the args that should be changed in the default args.
            use_cuda (optional): Use GPU if available. Setting to False will force model to use CPU only.
            cuda_device (optional): Specific GPU that should be used. Will use the first available GPU by default.
            **kwargs (optional): For providing proxies, force_download, resume_download, cache_dir and other options specific to the 'from_pretrained' implementation where this will be supplied.
        """  # noqa: ignore flake8"

        MODEL_CLASSES = {
            "gpt": (OpenAIGPTConfig, OpenAIGPTDoubleHeadsModel, OpenAIGPTTokenizer),
            "gpt2": (GPT2Config, GPT2DoubleHeadsModel, GPT2Tokenizer),
            "blender-small": (
                BlenderbotConfig,
                BlenderbotForConditionalGeneration,
                BlenderbotSmallTokenizer,
            ),
            "blender": (
                BlenderbotConfig,
                BlenderbotForConditionalGeneration,
                BlenderbotTokenizer,
            ),
        }

        self.args = self._load_model_args(model_name)

        if isinstance(args, dict):
            self.args.update_from_dict(args)
        elif isinstance(args, ConvAIArgs):
            self.args = args

        if "sweep_config" in kwargs:
            self.is_sweeping = True
            sweep_config = kwargs.pop("sweep_config")
            sweep_values = sweep_config_to_sweep_values(sweep_config)
            self.args.update_from_dict(sweep_values)
        else:
            self.is_sweeping = False

        if self.args.manual_seed:
            random.seed(self.args.manual_seed)
            np.random.seed(self.args.manual_seed)
            torch.manual_seed(self.args.manual_seed)
            if self.args.n_gpu > 0:
                torch.cuda.manual_seed_all(self.args.manual_seed)

        if not use_cuda:
            self.args.fp16 = False

        config_class, model_class, tokenizer_class = MODEL_CLASSES[model_type]
        self.__dict__.update(kwargs)
        self.config = config_class.from_pretrained(model_name, **self.args.config)

        if not self.args.quantized_model:
            self.model = model_class.from_pretrained(model_name, **kwargs)
        else:
            quantized_weights = torch.load(
                os.path.join(model_name, "pytorch_model.bin")
            )
            self.model = model_class.from_pretrained(
                None, config=self.config, state_dict=quantized_weights
            )

        self.tokenizer = tokenizer_class.from_pretrained(model_name, **kwargs)
        self.add_special_tokens_(self.model, self.tokenizer)

        if self.args.dynamic_quantize:
            self.model = torch.quantization.quantize_dynamic(
                self.model, {torch.nn.Linear}, dtype=torch.qint8
            )
        if self.args.quantized_model:
            self.model.load_state_dict(quantized_weights)
        if self.args.dynamic_quantize:
            self.args.quantized_model = True

        self.results = {}

        if use_cuda:
            if torch.cuda.is_available():
                if cuda_device == -1:
                    self.device = torch.device("cuda")
                else:
                    self.device = torch.device(f"cuda:{cuda_device}")
            else:
                raise ValueError(
                    "'use_cuda' set to True when cuda is unavailable."
                    " Make sure CUDA is available or set use_cuda=False."
                )
        else:
            self.device = "cpu"

        self.args.model_name = model_name
        self.args.model_type = model_type

        if self.args.wandb_project and not wandb_available:
            warnings.warn(
                "wandb_project specified but wandb is not available. Wandb disabled."
            )
            self.args.wandb_project = None

    def train_model(
        self,
        train_file=None,
        output_dir=None,
        show_running_loss=True,
        args=None,
        eval_file=None,
        verbose=True,
        **kwargs,
    ):
        """
        Trains the model using 'train_file'

        Args:
            train_file: Path to a JSON file containing the training data.
                If not given, train dataset from PERSONA-CHAT will be used.
            output_dir: The directory where model files will be saved. If not given, self.args.output_dir will be used.
            show_running_loss (optional): Set to False to prevent running loss from being printed to console. Defaults to True.
            args (optional): Optional changes to the args dict of the model. Any changes made will persist for the model.
            eval_file (optional): Evaluation data against which evaluation will be performed when evaluate_during_training is enabled.
                If not given when evaluate_during_training is enabled, the evaluation data from PERSONA-CHAT will be used.
            **kwargs:
        Returns:
            global_step: Number of global steps trained
            training_details: Average training loss if evaluate_during_training is False or full training progress scores if evaluate_during_training is True
        """  # noqa: ignore flake8"

        if self.args.model_type in ["blender-small", "blender"]:
            raise ValueError(
                "Fine-tuning of Blender models is not currently supported."
            )

        if args:
            self.args.update_from_dict(args)

        if self.args.silent:
            show_running_loss = False

        if self.args.evaluate_during_training and eval_file is None:
            warnings.warn(
                "eval_file not specified but evaluate_during_training is True. Using personachat eval data."
            )

        if not output_dir:
            output_dir = self.args.output_dir

        if (
            os.path.exists(output_dir)
            and os.listdir(output_dir)
            and not self.args.overwrite_output_dir
        ):
            raise ValueError(
                "Output directory ({}) already exists and is not empty."
                " Set overwrite_output_dir: True to automatically overwrite.".format(
                    output_dir
                )
            )
        self._move_model_to_device()

        train_dataloader, train_sampler = self.load_and_cache_examples(
            dataset_path=train_file,
            verbose=verbose,
            no_cache=self.args.no_cache or self.args.reprocess_input_data,
        )

        if self.args.evaluate_during_training:
            eval_loader, eval_sampler = self.load_and_cache_examples(
                dataset_path=eval_file,
                verbose=verbose,
                evaluate=True,
                no_cache=self.args.no_cache or self.args.reprocess_input_data,
            )
        else:
            eval_loader = None

        os.makedirs(output_dir, exist_ok=True)

        global_step, training_details = self.train(
            train_dataloader,
            output_dir,
            show_running_loss=show_running_loss,
            eval_dataloader=eval_loader,
            verbose=verbose,
            **kwargs,
        )

        self.save_model(model=self.model)

        if verbose:
            logger.info(
                " Training of {} model complete. Saved to {}.".format(
                    self.args.model_type, output_dir
                )
            )

    def train(
        self,
        train_dataloader,
        output_dir,
        show_running_loss=True,
        eval_dataloader=None,
        verbose=True,
        **kwargs,
    ):
        """
        Trains the model on train_dataset.

        Utility function to be used by the train_model() method. Not intended to be used directly.
        """

        device = self.device
        model = self.model
        args = self.args

        tb_writer = SummaryWriter(log_dir=args.tensorboard_dir)

        t_total = (
            len(train_dataloader)
            // args.gradient_accumulation_steps
            * args.num_train_epochs
        )

        no_decay = ["bias", "LayerNorm.weight"]

        optimizer_grouped_parameters = []
        custom_parameter_names = set()
        for group in self.args.custom_parameter_groups:
            params = group.pop("params")
            custom_parameter_names.update(params)
            param_group = {**group}
            param_group["params"] = [
                p for n, p in model.named_parameters() if n in params
            ]
            optimizer_grouped_parameters.append(param_group)

        for group in self.args.custom_layer_parameters:
            layer_number = group.pop("layer")
            layer = f"layer.{layer_number}."
            group_d = {**group}
            group_nd = {**group}
            group_nd["weight_decay"] = 0.0
            params_d = []
            params_nd = []
            for n, p in model.named_parameters():
                if n not in custom_parameter_names and layer in n:
                    if any(nd in n for nd in no_decay):
                        params_nd.append(p)
                    else:
                        params_d.append(p)
                    custom_parameter_names.add(n)
            group_d["params"] = params_d
            group_nd["params"] = params_nd

            optimizer_grouped_parameters.append(group_d)
            optimizer_grouped_parameters.append(group_nd)

        if not self.args.train_custom_parameters_only:
            optimizer_grouped_parameters.extend(
                [
                    {
                        "params": [
                            p
                            for n, p in model.named_parameters()
                            if n not in custom_parameter_names
                            and not any(nd in n for nd in no_decay)
                        ],
                        "weight_decay": args.weight_decay,
                    },
                    {
                        "params": [
                            p
                            for n, p in model.named_parameters()
                            if n not in custom_parameter_names
                            and any(nd in n for nd in no_decay)
                        ],
                        "weight_decay": 0.0,
                    },
                ]
            )

        warmup_steps = math.ceil(t_total * args.warmup_ratio)
        args.warmup_steps = (
            warmup_steps if args.warmup_steps == 0 else args.warmup_steps
        )

        if args.optimizer == "AdamW":
            optimizer = AdamW(
                optimizer_grouped_parameters,
                lr=args.learning_rate,
                eps=args.adam_epsilon,
            )
        elif args.optimizer == "Adafactor":
            optimizer = Adafactor(
                optimizer_grouped_parameters,
                lr=args.learning_rate,
                eps=args.adafactor_eps,
                clip_threshold=args.adafactor_clip_threshold,
                decay_rate=args.adafactor_decay_rate,
                beta1=args.adafactor_beta1,
                weight_decay=args.weight_decay,
                scale_parameter=args.adafactor_scale_parameter,
                relative_step=args.adafactor_relative_step,
                warmup_init=args.adafactor_warmup_init,
            )
            print("Using Adafactor for T5")
        else:
            raise ValueError(
                "{} is not a valid optimizer class. Please use one of ('AdamW', 'Adafactor') instead.".format(
                    args.optimizer
                )
            )

        if args.scheduler == "constant_schedule":
            scheduler = get_constant_schedule(optimizer)

        elif args.scheduler == "constant_schedule_with_warmup":
            scheduler = get_constant_schedule_with_warmup(
                optimizer, num_warmup_steps=args.warmup_steps
            )

        elif args.scheduler == "linear_schedule_with_warmup":
            scheduler = get_linear_schedule_with_warmup(
                optimizer,
                num_warmup_steps=args.warmup_steps,
                num_training_steps=t_total,
            )

        elif args.scheduler == "cosine_schedule_with_warmup":
            scheduler = get_cosine_schedule_with_warmup(
                optimizer,
                num_warmup_steps=args.warmup_steps,
                num_training_steps=t_total,
                num_cycles=args.cosine_schedule_num_cycles,
            )

        elif args.scheduler == "cosine_with_hard_restarts_schedule_with_warmup":
            scheduler = get_cosine_with_hard_restarts_schedule_with_warmup(
                optimizer,
                num_warmup_steps=args.warmup_steps,
                num_training_steps=t_total,
                num_cycles=args.cosine_schedule_num_cycles,
            )

        elif args.scheduler == "polynomial_decay_schedule_with_warmup":
            scheduler = get_polynomial_decay_schedule_with_warmup(
                optimizer,
                num_warmup_steps=args.warmup_steps,
                num_training_steps=t_total,
                lr_end=args.polynomial_decay_schedule_lr_end,
                power=args.polynomial_decay_schedule_power,
            )

        else:
            raise ValueError("{} is not a valid scheduler.".format(args.scheduler))

        if args.n_gpu > 1:
            model = torch.nn.DataParallel(model)

        global_step = 0
        training_progress_scores = None
        tr_loss, logging_loss = 0.0, 0.0
        model.zero_grad()
        train_iterator = trange(
            int(args.num_train_epochs), desc="Epoch", disable=args.silent
        )
        epoch_number = 0
        best_eval_metric = None
        early_stopping_counter = 0

        if args.evaluate_during_training:
            training_progress_scores = self._create_training_progress_scores(**kwargs)

        if args.wandb_project:
            wandb.init(
                project=args.wandb_project,
                config={**asdict(args)},
                **args.wandb_kwargs,
            )
            wandb.run._label(repo="simpletransformers")
            wandb.watch(self.model)
            self.wandb_run_id = wandb.run.id

        if args.fp16:
            from torch.cuda import amp

            scaler = amp.GradScaler()

        for _ in train_iterator:
            model.train()
            train_iterator.set_description(
                f"Epoch {epoch_number + 1} of {args.num_train_epochs}"
            )
            batch_iterator = tqdm(
                train_dataloader,
                desc=f"Running Epoch {epoch_number} of {args.num_train_epochs}",
                disable=args.silent,
                mininterval=0,
            )
            for step, batch in enumerate(batch_iterator):
                batch = tuple(t.to(device) for t in batch)
                input_ids, mc_token_ids, labels, mc_labels, token_type_ids = batch

                if args.fp16:
                    with amp.autocast():
                        outputs = model(
                            input_ids,
                            token_type_ids=token_type_ids,
                            mc_token_ids=mc_token_ids,
                            mc_labels=mc_labels,
                            labels=labels,
                        )

                        lm_loss, mc_loss = outputs[:2]
                        # model outputs are always tuple in pytorch-transformers (see doc)
                        loss = lm_loss * args.lm_coef + mc_loss * args.mc_coef
                else:
                    outputs = model(
                        input_ids,
                        token_type_ids=token_type_ids,
                        mc_token_ids=mc_token_ids,
                        mc_labels=mc_labels,
                        labels=labels,
                    )

                    lm_loss, mc_loss = outputs[:2]
                    # model outputs are always tuple in pytorch-transformers (see doc)
                    loss = lm_loss * args.lm_coef + mc_loss * args.mc_coef

                if args.n_gpu > 1:
                    loss = (
                        loss.mean()
                    )  # mean() to average on multi-gpu parallel training

                current_loss = loss.item()

                if show_running_loss:
                    print("\rRunning loss: %f" % current_loss, end="")

                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps

                if args.fp16:
                    scaler.scale(loss).backward()
                else:
                    loss.backward()

                tr_loss += loss.item()
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    if args.fp16:
                        scaler.unscale_(optimizer)
                    if args.optimizer == "AdamW":
                        torch.nn.utils.clip_grad_norm_(
                            model.parameters(), args.max_grad_norm
                        )

                    if args.fp16:
                        scaler.step(optimizer)
                        scaler.update()
                    else:
                        optimizer.step()
                    scheduler.step()  # Update learning rate schedule
                    model.zero_grad()
                    global_step += 1

                    if args.logging_steps > 0 and global_step % args.logging_steps == 0:
                        # Log metrics
                        tb_writer.add_scalar(
                            "lr", scheduler.get_last_lr()[0], global_step
                        )
                        tb_writer.add_scalar(
                            "loss",
                            (tr_loss - logging_loss) / args.logging_steps,
                            global_step,
                        )
                        logging_loss = tr_loss
                        if args.wandb_project or self.is_sweeping:
                            wandb.log(
                                {
                                    "Training loss": current_loss,
                                    "lr": scheduler.get_last_lr()[0],
                                    "global_step": global_step,
                                }
                            )

                    if args.save_steps > 0 and global_step % args.save_steps == 0:
                        # Save model checkpoint
                        output_dir_current = os.path.join(
                            output_dir, "checkpoint-{}".format(global_step)
                        )

                        self.save_model(output_dir_current, model=model)

                    if args.evaluate_during_training and (
                        args.evaluate_during_training_steps > 0
                        and global_step % args.evaluate_during_training_steps == 0
                    ):
                        # Only evaluate when single GPU otherwise metrics may not average well
                        results, _, _ = self.eval_model(
                            eval_dataloader,
                            verbose=verbose and args.evaluate_during_training_verbose,
                            silent=args.evaluate_during_training_silent,
                            **kwargs,
                        )
                        for key, value in results.items():
                            try:
                                tb_writer.add_scalar(
                                    "eval_{}".format(key), value, global_step
                                )
                            except (NotImplementedError, AssertionError):
                                pass

                        output_dir_current = os.path.join(
                            output_dir, "checkpoint-{}".format(global_step)
                        )

                        if args.save_eval_checkpoints:
                            self.save_model(
                                output_dir_current, model=model, results=results
                            )

                        training_progress_scores["global_step"].append(global_step)
                        training_progress_scores["train_loss"].append(current_loss)
                        for key in results:
                            training_progress_scores[key].append(results[key])
                        report = pd.DataFrame(training_progress_scores)
                        report.to_csv(
                            os.path.join(
                                args.output_dir, "training_progress_scores.csv"
                            ),
                            index=False,
                        )

                        if args.wandb_project or self.is_sweeping:
                            wandb.log(self._get_last_metrics(training_progress_scores))

                        if not best_eval_metric:
                            best_eval_metric = results[args.early_stopping_metric]
                            self.save_model(
                                args.best_model_dir, model=model, results=results
                            )
                        if best_eval_metric and args.early_stopping_metric_minimize:
                            if (
                                results[args.early_stopping_metric] - best_eval_metric
                                < args.early_stopping_delta
                            ):
                                best_eval_metric = results[args.early_stopping_metric]
                                self.save_model(
                                    args.best_model_dir, model=model, results=results
                                )
                                early_stopping_counter = 0
                            else:
                                if args.use_early_stopping:
                                    if (
                                        early_stopping_counter
                                        < args.early_stopping_patience
                                    ):
                                        early_stopping_counter += 1
                                        if verbose:
                                            logger.info(
                                                f" No improvement in {args.early_stopping_metric}"
                                            )
                                            logger.info(
                                                f" Current step: {early_stopping_counter}"
                                            )
                                            logger.info(
                                                f" Early stopping patience: {args.early_stopping_patience}"
                                            )
                                    else:
                                        if verbose:
                                            logger.info(
                                                f" Patience of {args.early_stopping_patience} steps reached"
                                            )
                                            logger.info(" Training terminated.")
                                            train_iterator.close()
                                        return (
                                            global_step,
                                            tr_loss / global_step
                                            if not self.args.evaluate_during_training
                                            else training_progress_scores,
                                        )
                        else:
                            if (
                                results[args.early_stopping_metric] - best_eval_metric
                                > args.early_stopping_delta
                            ):
                                best_eval_metric = results[args.early_stopping_metric]
                                self.save_model(
                                    args.best_model_dir, model=model, results=results
                                )
                                early_stopping_counter = 0
                            else:
                                if args.use_early_stopping:
                                    if (
                                        early_stopping_counter
                                        < args.early_stopping_patience
                                    ):
                                        early_stopping_counter += 1
                                        if verbose:
                                            logger.info(
                                                f" No improvement in {args.early_stopping_metric}"
                                            )
                                            logger.info(
                                                f" Current step: {early_stopping_counter}"
                                            )
                                            logger.info(
                                                f" Early stopping patience: {args.early_stopping_patience}"
                                            )
                                    else:
                                        if verbose:
                                            logger.info(
                                                f" Patience of {args.early_stopping_patience} steps reached"
                                            )
                                            logger.info(" Training terminated.")
                                            train_iterator.close()
                                        return (
                                            global_step,
                                            tr_loss / global_step
                                            if not self.args.evaluate_during_training
                                            else training_progress_scores,
                                        )

            epoch_number += 1
            output_dir_current = os.path.join(
                output_dir, "checkpoint-{}-epoch-{}".format(global_step, epoch_number)
            )

            if args.save_model_every_epoch or args.evaluate_during_training:
                os.makedirs(output_dir_current, exist_ok=True)

            if args.save_model_every_epoch:
                self.save_model(output_dir_current, model=model)

            if args.evaluate_during_training and args.evaluate_each_epoch:
                results, _, _ = self.eval_model(
                    eval_dataloader,
                    verbose=verbose and args.evaluate_during_training_verbose,
                    silent=True,
                    **kwargs,
                )

                self.save_model(output_dir_current, results=results)

                training_progress_scores["global_step"].append(global_step)
                training_progress_scores["train_loss"].append(current_loss)
                for key in results:
                    training_progress_scores[key].append(results[key])
                report = pd.DataFrame(training_progress_scores)
                report.to_csv(
                    os.path.join(args.output_dir, "training_progress_scores.csv"),
                    index=False,
                )

                if args.wandb_project or self.is_sweeping:
                    wandb.log(self._get_last_metrics(training_progress_scores))

                if not best_eval_metric:
                    best_eval_metric = results[args.early_stopping_metric]
                    self.save_model(args.best_model_dir, model=model, results=results)
                if best_eval_metric and args.early_stopping_metric_minimize:
                    if (
                        results[args.early_stopping_metric] - best_eval_metric
                        < args.early_stopping_delta
                    ):
                        best_eval_metric = results[args.early_stopping_metric]
                        self.save_model(
                            args.best_model_dir, model=model, results=results
                        )
                        early_stopping_counter = 0
                else:
                    if (
                        results[args.early_stopping_metric] - best_eval_metric
                        > args.early_stopping_delta
                    ):
                        best_eval_metric = results[args.early_stopping_metric]
                        self.save_model(
                            args.best_model_dir, model=model, results=results
                        )
                        early_stopping_counter = 0
                model.train()

        return (
            global_step,
            tr_loss / global_step
            if not self.args.evaluate_during_training
            else training_progress_scores,
        )

    def eval_model(
        self, eval_file=None, output_dir=None, verbose=True, silent=False, **kwargs
    ):
        """
        Evaluates the model on eval_file. Saves results to output_dir.

        Args:
            eval_file: Path to a JSON file containing the evaluation data.
                If not given, eval dataset from PERSONA-CHAT will be used.
            output_dir: The directory where model files will be saved. If not given, self.args.output_dir will be used.
            verbose: If verbose, results will be printed to the console on completion of evaluation.
            silent: If silent, tqdm progress bars will be hidden.
            **kwargs: Additional metrics that should be used. Pass in the metrics as keyword arguments (name of metric: function to use). E.g. f1=sklearn.metrics.f1_score.
                        A metric function should take in two parameters. The first parameter will be the true labels, and the second parameter will be the predictions.

        Returns:
            result: Dictionary containing evaluation results. (f1_score, language_model_loss)
        """  # noqa: ignore flake8"

        if not output_dir:
            output_dir = self.args.output_dir

        self._move_model_to_device()

        result = self.evaluate(
            eval_file, output_dir, verbose=verbose, silent=silent, **kwargs
        )
        self.results.update(result)

        if verbose:
            logger.info(self.results)

        return result

    def evaluate(self, eval_file, output_dir, verbose=True, silent=False, **kwargs):
        """
        Evaluates the model on eval_file.

        Utility function to be used by the eval_model() method. Not intended to be used directly.
        """
        device = self.device
        model = self.model
        args = self.args
        eval_output_dir = output_dir

        eval_dataloader, eval_sampler = self.load_and_cache_examples(
            eval_file,
            evaluate=True,
            verbose=verbose,
            silent=silent,
            no_cache=self.args.no_cache or self.args.use_cached_eval_features,
        )
        os.makedirs(eval_output_dir, exist_ok=True)

        nb_eval_steps = 0
        loss_fct = torch.nn.CrossEntropyLoss(ignore_index=-100)
        results = {
            "language_model_loss": [],
            "f1_score": [],
        }
        model.eval()

        if args.n_gpu > 1:
            model = torch.nn.DataParallel(model)

        if args.fp16:
            from torch.cuda import amp

        for batch in tqdm(
            eval_dataloader, disable=args.silent or silent, desc="Running Evaluation"
        ):
            batch = tuple(t.to(device) for t in batch)

            with torch.no_grad():
                input_ids, mc_token_ids, labels, mc_labels, token_type_ids = batch

                if args.fp16:
                    with amp.autocast():
                        outputs = model(
                            input_ids,
                            token_type_ids=token_type_ids,
                            mc_token_ids=mc_token_ids,
                        )
                        lm_logits, mc_logits = outputs[:2]
                else:
                    outputs = model(
                        input_ids,
                        token_type_ids=token_type_ids,
                        mc_token_ids=mc_token_ids,
                    )
                    lm_logits, mc_logits = outputs[:2]
                # model outputs are always tuple in pytorch-transformers (see doc)

                lm_logits_flat_shifted = (
                    lm_logits[..., :-1, :].contiguous().view(-1, lm_logits.size(-1))
                )
                labels_flat_shifted = labels[..., 1:].contiguous().view(-1)

            nb_eval_steps += 1

            mc_logits = [np.argmax(pred) for pred in mc_logits.cpu().numpy()]
            f1_current = f1_score(mc_labels.cpu().numpy(), mc_logits, average="macro")
            lm_loss_current = loss_fct(lm_logits_flat_shifted, labels_flat_shifted)

            results["language_model_loss"].append(lm_loss_current.cpu().numpy().item())
            results["f1_score"].append(f1_current)

        results["language_model_loss"] = statistics.mean(results["language_model_loss"])
        results["f1_score"] = statistics.mean(results["f1_score"])

        output_eval_file = os.path.join(eval_output_dir, "eval_results.txt")
        with open(output_eval_file, "w") as writer:
            for key in sorted(results.keys()):
                writer.write("{} = {}\n".format(key, str(results[key])))

        return results

    def load_and_cache_examples(
        self,
        dataset_path=None,
        evaluate=False,
        no_cache=False,
        verbose=True,
        silent=False,
    ):
        """
        Loads, tokenizes, and prepares data for training and/or evaluation.

        Utility function for train() and eval() methods. Not intended to be used directly.
        """  # noqa: ignore flake8"

        process_count = self.args.process_count

        tokenizer = self.tokenizer
        args = self.args

        if not no_cache:
            no_cache = args.no_cache

        os.makedirs(self.args.cache_dir, exist_ok=True)

        dataset_path = dataset_path if dataset_path else ""

        dataset = get_dataset(
            tokenizer,
            dataset_path,
            args.cache_dir,
            process_count=process_count,
            proxies=self.__dict__.get("proxies", None),
            evaluate=evaluate,
            no_cache=no_cache,
            args=args,
        )
        # logger.info(personachat.keys())
        # datasets = {"train": defaultdict(list), "valid": defaultdict(list)}
        # for dataset_name, dataset in personachat.items():
        datasets = defaultdict(list)
        num_candidates = len(dataset[0]["utterances"][0]["candidates"])
        if args.num_candidates > 0 and not evaluate:
            num_candidates = min(args.num_candidates, num_candidates)
        for dialog in dataset:
            persona = dialog["personality"].copy()
            for _ in range(args.personality_permutations):
                for utterance in dialog["utterances"]:
                    history = utterance["history"][-(2 * args.max_history + 1) :]
                    for j, candidate in enumerate(
                        utterance["candidates"][-num_candidates:]
                    ):
                        labels = bool(j == num_candidates - 1)
                        instance = self.build_input_from_segments(
                            persona, history, candidate, tokenizer, labels
                        )
                        for input_name, input_array in instance.items():
                            datasets[input_name].append(input_array)
                    datasets["mc_labels"].append(num_candidates - 1)
                    datasets["n_candidates"] = num_candidates
                persona = [persona[-1]] + persona[:-1]  # permuted personalities

        # logger.info(" Pad inputs and convert to Tensor")
        # tensor_datasets = {"train": [], "valid": []}
        # for dataset_name, dataset in datasets.items():
        tensor_datasets = []
        dataset = self.pad_dataset(
            datasets, padding=tokenizer.convert_tokens_to_ids(SPECIAL_TOKENS[-1])
        )
        for input_name in MODEL_INPUTS:
            tensor = torch.tensor(dataset[input_name])
            if input_name != "mc_labels":
                tensor = tensor.view((-1, datasets["n_candidates"]) + tensor.shape[1:])
            tensor_datasets.append(tensor)

        # logger.info(" Build train and validation dataloaders")
        # train_dataset, valid_dataset = (
        #     TensorDataset(*tensor_datasets["train"]),
        #     TensorDataset(*tensor_datasets["valid"]),
        # )
        tensor_dataset = TensorDataset(*tensor_datasets)
        if not evaluate:
            data_sampler = RandomSampler(tensor_dataset)
            data_loader = DataLoader(
                tensor_dataset, sampler=data_sampler, batch_size=args.train_batch_size
            )
        else:
            data_sampler = SequentialSampler(tensor_dataset)
            data_loader = DataLoader(
                tensor_dataset, sampler=data_sampler, batch_size=args.eval_batch_size
            )

        # logger.info(" Train dataset (Batch, Candidates, Seq length): {}".format(train_dataset.tensors[0].shape))
        # logger.info(" valid dataset (Batch, Candidates, Seq length): {}".format(valid_dataset.tensors[0].shape))
        return data_loader, data_sampler

    def compute_metrics(self, mc_preds, mc_labels, lm_logits, labels, **kwargs):
        """
        Computes the evaluation metrics for the model predictions.

        Args:
            mc_preds: Model next sentence predictions.
            mc_labels: Ground truth next sentence.
            lm_logits: Language model logits.
            labels: Language model ground truth.
            eval_examples: List of examples on which evaluation was performed
            **kwargs: Additional metrics that should be used. Pass in the metrics as keyword arguments (name of metric: function to use). E.g. f1=sklearn.metrics.f1_score.
                        A metric function should take in two parameters. The first parameter will be the true labels, and the second parameter will be the predictions.

        Returns:
            result: Dictionary containing evaluation results. (f1_score, language_model_loss)
        """  # noqa: ignore flake8"

        loss_fct = torch.nn.CrossEntropyLoss(ignore_index=-100)
        extra_metrics = {}
        for metric, func in kwargs.items():
            extra_metrics[metric] = func(mc_labels, mc_preds)

        f1_current = f1_score(mc_labels.cpu().numpy(), mc_preds, average="macro")
        lm_loss_current = loss_fct(lm_logits, labels)

        return {
            **{"f1_score": f1_current, "language_model_loss": lm_loss_current},
            **extra_metrics,
        }

    def interact(self, personality=None):
        """
        Interact with a model in the terminal.

        Args:
            personality: A list of sentences that the model will use to build a personality.

        Returns:
            None
        """

        model = self.model
        args = self.args
        tokenizer = self.tokenizer
        process_count = self.args.process_count

        if self.args.fp16:
            from torch.cuda import amp

        self._move_model_to_device()

        if self.args.model_type in ["blender", "blender-small"]:
            if not personality:
                personality = []
        else:
            if not personality:
                dataset = get_dataset(
                    tokenizer,
                    None,
                    args.cache_dir,
                    process_count=process_count,
                    proxies=self.__dict__.get("proxies", None),
                    interact=True,
                    args=args,
                )
                personalities = [
                    dialog["personality"]
                    for dataset in dataset.values()
                    for dialog in dataset
                ]
                personality = random.choice(personalities)
            else:
                personality = [tokenizer.encode(s.lower()) for s in personality]

        history = []
        while True:
            raw_text = input(">>> ")
            while not raw_text:
                print("Prompt should not be empty!")
                raw_text = input(">>> ")
            history.append(
                tokenizer.encode(raw_text)
                if self.args.model_type not in ["blender", "blender-small"]
                else raw_text
            )
            with torch.no_grad():
                if args.fp16:
                    with amp.autocast():
                        out_ids = self.sample_sequence(
                            personality, history, tokenizer, model, args
                        )
                else:
                    out_ids = self.sample_sequence(
                        personality, history, tokenizer, model, args
                    )
            history.append(out_ids)
            history = history[-(2 * args.max_history + 1) :]
            if self.args.model_type in ["blender", "blender-small"]:
                out_text = out_ids
            else:
                out_text = tokenizer.decode(
                    out_ids, skip_special_tokens=self.args.skip_special_tokens
                )
            print(out_text)
            print(history)

    def interact_single(self, message, history, personality=None, encode_history=True):
        """
        Get Response from the model based on the history and message

        Args:
            message: A message to be sent to the model.
            history: A list of sentences that repersents the interaction history between the model and the user.
            personality (optional): A list of sentences that the model will use to build a personality.
            encode_history (optional): If True, the history should be in text (string) form.
                            The history will be tokenized and encoded.

        Returns:
            out_text: The response generated by the model based on the personality, history and message.
            history: The updated history of the conversation. If encode_history is True, this will be in text form.
                        If not, it will be in encoded form.
        """
        model = self.model
        args = self.args
        tokenizer = self.tokenizer
        process_count = self.args.process_count

        if self.args.fp16:
            from torch.cuda import amp

        self._move_model_to_device()

        if not personality:
            dataset = get_dataset(
                tokenizer,
                None,
                args.cache_dir,
                process_count=process_count,
                proxies=self.__dict__.get("proxies", None),
                interact=True,
            )
            personalities = [
                dialog["personality"]
                for dataset in dataset.values()
                for dialog in dataset
            ]
            personality = random.choice(personalities)
        else:
            personality = [tokenizer.encode(s.lower()) for s in personality]

        if encode_history:
            raw_history = history.copy()
            raw_history.append(message)
            history = [tokenizer.encode(sentence) for sentence in history]
        history.append(tokenizer.encode(message))
        with torch.no_grad():
            if args.fp16:
                with amp.autocast():
                    out_ids = self.sample_sequence(
                        personality, history, tokenizer, model, args
                    )
            else:
                out_ids = self.sample_sequence(
                    personality, history, tokenizer, model, args
                )
        out_text = tokenizer.decode(
            out_ids, skip_special_tokens=self.args.skip_special_tokens
        )

        if encode_history:
            raw_history.append(out_text)
            history = raw_history
        else:
            history.append(out_ids)

        return out_text, history

    def _threshold(self, x, threshold):
        if x >= threshold:
            return 1
        return 0

    def _move_model_to_device(self):
        self.model.to(self.device)

    # def _get_inputs_dict(self, batch):
    #     input_ids, mc_token_ids, labels, mc_labels, token_type_ids = batch

    #     return input_ids, mc_token_ids, labels, mc_labels, token_type_ids

    def _get_last_metrics(self, metric_values):
        return {metric: values[-1] for metric, values in metric_values.items()}

    def _create_training_progress_scores(self, **kwargs):
        extra_metrics = {key: [] for key in kwargs}
        training_progress_scores = {
            "global_step": [],
            "language_model_loss": [],
            "f1_score": [],
            **extra_metrics,
        }

        return training_progress_scores

    def save_model(self, output_dir=None, model=None, results=None):
        if not output_dir:
            output_dir = self.args.output_dir

        if model and not self.args.no_save:
            # Take care of distributed/parallel training
            model_to_save = model.module if hasattr(model, "module") else model
            model_to_save.save_pretrained(output_dir)
            self.tokenizer.save_pretrained(output_dir)
            self.save_model_args(output_dir)

        if results:
            output_eval_file = os.path.join(output_dir, "eval_results.txt")
            with open(output_eval_file, "w") as writer:
                for key in sorted(results.keys()):
                    writer.write("{} = {}\n".format(key, str(results[key])))

    def add_special_tokens_(self, model, tokenizer):
        """Add special tokens to the tokenizer and the model if they have not already been added."""
        orig_num_tokens = len(tokenizer.encoder)
        num_added_tokens = tokenizer.add_special_tokens(
            ATTR_TO_SPECIAL_TOKEN
        )  # doesn't add if they are already there
        if num_added_tokens > 0:
            self.model.resize_token_embeddings(
                new_num_tokens=orig_num_tokens + num_added_tokens
            )

    def build_input_from_segments(
        self, persona, history, reply, tokenizer, labels=False, with_eos=True
    ):
        """Build a sequence of input from 3 segments: persona, history and last reply."""
        bos, eos, speaker1, speaker2 = tokenizer.convert_tokens_to_ids(
            SPECIAL_TOKENS[:-1]
        )
        sequence = (
            [[bos] + list(chain(*persona))]
            + history
            + [reply + ([eos] if with_eos else [])]
        )
        sequence = [sequence[0]] + [
            [speaker2 if (len(sequence) - i) % 2 else speaker1] + s
            for i, s in enumerate(sequence[1:])
        ]
        instance = {}
        instance["input_ids"] = list(chain(*sequence))
        instance["token_type_ids"] = [
            speaker2 if i % 2 else speaker1 for i, s in enumerate(sequence) for _ in s
        ]
        instance["mc_token_ids"] = len(instance["input_ids"]) - 1
        instance["labels"] = [-100] * len(instance["input_ids"])
        if labels:
            instance["labels"] = (
                ([-100] * sum(len(s) for s in sequence[:-1]))
                + [-100]
                + sequence[-1][1:]
            )
        return instance

    def pad_dataset(self, dataset, padding=0):
        """Pad the dataset. This could be optimized by defining a Dataset class and padding at the batch level,
        but this is simpler."""
        max_l = max(len(x) for x in dataset["input_ids"])
        for name in PADDED_INPUTS:
            dataset[name] = [
                x + [padding if name != "labels" else -100] * (max_l - len(x))
                for x in dataset[name]
            ]
        return dataset

    def top_filtering(
        self,
        logits,
        top_k=0.0,
        top_p=0.9,
        threshold=-float("Inf"),
        filter_value=-float("Inf"),
    ):
        """Filter a distribution of logits using top-k, top-p (nucleus) and/or threshold filtering
        Args:
            logits: logits distribution shape (vocabulary size)
            top_k: <=0: no filtering, >0: keep only top k tokens with highest probability.
            top_p: <=0.0: no filtering, >0.0: keep only a subset S of candidates, where S is the smallest subset
                whose total probability mass is greater than or equal to the threshold top_p.
                In practice, we select the highest probability tokens whose cumulative probability mass exceeds
                the threshold top_p.
            threshold: a minimal threshold to keep logits
        """
        assert (
            logits.dim() == 1
        )  # Only work for batch size 1 for now - could update but it would obfuscate a bit the code
        top_k = min(top_k, logits.size(-1))
        if top_k > 0:
            # Remove all tokens with a probability less than the last token in the top-k tokens
            indices_to_remove = logits < torch.topk(logits, top_k)[0][..., -1, None]
            logits[indices_to_remove] = filter_value

        if top_p > 0.0:
            # Compute cumulative probabilities of sorted tokens
            sorted_logits, sorted_indices = torch.sort(logits, descending=True)
            cumulative_probabilities = torch.cumsum(
                F.softmax(sorted_logits, dim=-1), dim=-1
            )

            # Remove tokens with cumulative probability above the threshold
            sorted_indices_to_remove = cumulative_probabilities > top_p
            # Shift the indices to the right to keep also the first token above the threshold
            sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[
                ..., :-1
            ].clone()
            sorted_indices_to_remove[..., 0] = 0

            # Back to unsorted indices and set them to -infinity
            indices_to_remove = sorted_indices[sorted_indices_to_remove]
            logits[indices_to_remove] = filter_value

        indices_to_remove = logits < threshold
        logits[indices_to_remove] = filter_value

        return logits

    def sample_sequence(
        self, personality, history, tokenizer, model, args, current_output=None
    ):
        if self.args.model_type in ["blender", "blender-small"]:
            print("Input >>>>>>> ", "\n".join(personality) + "\n" + "\n".join(history))
            print("---------------------------------")
            inputs = self.tokenizer(
                ["\n".join(history).strip("\n")], return_tensors="pt"
            )
            # inputs = self.tokenizer(["\n".join(personality) + "\n" + "\n".join(history)], return_tensors='pt')
            inputs["input_ids"] = inputs["input_ids"].to(self.device)
            inputs["attention_mask"] = inputs["attention_mask"].to(self.device)
            reply_ids = self.model.generate(**inputs)
            reply = [
                tokenizer.decode(
                    g, skip_special_tokens=True, clean_up_tokenization_spaces=True
                )
                for g in reply_ids
            ]
            return (
                reply[0][10:-8] if self.args.model_type == "blender-small" else reply[0]
            )  # To remove the "__start__ " and " __end__"
        else:
            special_tokens_ids = tokenizer.convert_tokens_to_ids(SPECIAL_TOKENS)
            if current_output is None:
                current_output = []

            for i in range(args.max_length):
                instance = self.build_input_from_segments(
                    personality, history, current_output, tokenizer, with_eos=False
                )

                input_ids = torch.tensor(
                    instance["input_ids"], device=self.device
                ).unsqueeze(0)
                token_type_ids = torch.tensor(
                    instance["token_type_ids"], device=self.device
                ).unsqueeze(0)

                logits = model(input_ids, token_type_ids=token_type_ids)
                if args.model_type in ["gpt2", "gpt"]:  # for gpt2 and maybe others
                    logits = logits[0]
                logits = logits[0, -1, :] / args.temperature
                logits = self.top_filtering(logits, top_k=args.top_k, top_p=args.top_p)
                probs = F.softmax(logits, dim=-1)

                prev = (
                    torch.topk(probs, 1)[1]
                    if not args.do_sample
                    else torch.multinomial(probs, 1)
                )
                if i < args.min_length and prev.item() in special_tokens_ids:
                    while prev.item() in special_tokens_ids:
                        if probs.max().item() == 1:
                            warnings.warn(
                                "Warning: model generating special token with probability 1."
                            )
                            break  # avoid infinitely looping over special token
                        prev = torch.multinomial(probs, num_samples=1)

                if prev.item() in special_tokens_ids:
                    break
                current_output.append(prev.item())

        return current_output

    def save_model_args(self, output_dir):
        os.makedirs(output_dir, exist_ok=True)
        self.args.save(output_dir)

    def _load_model_args(self, input_dir):
        args = ConvAIArgs()
        args.load(input_dir)
        return args

    def get_named_parameters(self):
        return [n for n, p in self.model.named_parameters()]
