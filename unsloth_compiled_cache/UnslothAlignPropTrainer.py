"""
2025.3.8
2025.3.9
4.49.0
0.9.6
__UNSLOTH_VERSIONING__
"""
from torch import Tensor
import torch
import torch.nn as nn
from torch.nn import functional as F
from trl.trainer.alignprop_trainer import (Accelerator, AlignPropConfig, AlignPropTrainer, Any, BaseTrainer, Callable, DDPOStableDiffusionPipeline, MODEL_CARD_TEMPLATE, Optional, ProjectConfiguration, Tuple, defaultdict, logger, os, set_seed, torch, warn, warnings, whoami)


import os
from typing import *
from dataclasses import dataclass, field
from packaging.version import Version
import torch
import numpy as np
from contextlib import nullcontext
from torch.nn import functional as F
torch_compile_options = {
    "epilogue_fusion"   : True,
    "max_autotune"      : False,
    "shape_padding"     : True,
    "trace.enabled"     : False,
    "triton.cudagraphs" : False,
}

@torch.compile(dynamic = True, fullgraph = True, options = torch_compile_options,)
def selective_log_softmax(logits, index):
    logits = logits.to(torch.float32)
    selected_logits = torch.gather(logits, dim = -1, index = index.unsqueeze(-1)).squeeze(-1)
    # loop to reduce peak mem consumption
    # logsumexp_values = torch.stack([torch.logsumexp(lg, dim=-1) for lg in logits])
    logsumexp_values = torch.logsumexp(logits, dim = -1)
    per_token_logps = selected_logits - logsumexp_values  # log_softmax(x_i) = x_i - logsumexp(x)
    return per_token_logps
@dataclass
class UnslothAlignPropConfig(AlignPropConfig):
    """
    
    Configuration class for AlignPropTrainer
    
    """
    vllm_sampling_params: Optional[Any] = field(
        default = None,
        metadata = {'help': 'vLLM SamplingParams'},
    )
    unsloth_num_chunks : Optional[int] = field(
        default = -1,
        metadata = {'help': 'Chunk size to reduce memory usage. -1 is most efficient.'},
    )
    def __init__(
        self,
        exp_name = 'llamafactory-',
        run_name = '',
        seed = 3407,
        log_with = None,
        tracker_project_name = 'trl',
        logdir = 'logs',
        num_epochs = 100,
        save_freq = 1,
        num_checkpoint_limit = 5,
        mixed_precision = 'fp16',
        allow_tf32 = True,
        resume_from = '',
        sample_num_steps = 50,
        sample_eta = 1.0,
        sample_guidance_scale = 5.0,
        train_batch_size = 1,
        train_use_8bit_adam = False,
        train_learning_rate = 5e-05,
        train_adam_beta1 = 0.9,
        train_adam_beta2 = 0.999,
        train_adam_weight_decay = 0.01,
        train_adam_epsilon = 1e-08,
        train_gradient_accumulation_steps = 2,
        train_max_grad_norm = 1.0,
        negative_prompts = '',
        truncated_backprop_rand = True,
        truncated_backprop_timestep = 49,
        vllm_sampling_params = None,
        unsloth_num_chunks = -1,
        **kwargs,
    ):
        
        super().__init__(
            exp_name = exp_name,
            run_name = run_name,
            seed = seed,
            log_with = log_with,
            tracker_project_name = tracker_project_name,
            logdir = logdir,
            num_epochs = num_epochs,
            save_freq = save_freq,
            num_checkpoint_limit = num_checkpoint_limit,
            mixed_precision = mixed_precision,
            allow_tf32 = allow_tf32,
            resume_from = resume_from,
            sample_num_steps = sample_num_steps,
            sample_eta = sample_eta,
            sample_guidance_scale = sample_guidance_scale,
            train_batch_size = train_batch_size,
            train_use_8bit_adam = train_use_8bit_adam,
            train_learning_rate = train_learning_rate,
            train_adam_beta1 = train_adam_beta1,
            train_adam_beta2 = train_adam_beta2,
            train_adam_weight_decay = train_adam_weight_decay,
            train_adam_epsilon = train_adam_epsilon,
            train_gradient_accumulation_steps = train_gradient_accumulation_steps,
            train_max_grad_norm = train_max_grad_norm,
            negative_prompts = negative_prompts,
            truncated_backprop_rand = truncated_backprop_rand,
            truncated_backprop_timestep = truncated_backprop_timestep,**kwargs)
        self.vllm_sampling_params = vllm_sampling_params
        self.unsloth_num_chunks = unsloth_num_chunks
pass

class _UnslothAlignPropTrainer(BaseTrainer):
    """"""

    _tag_names = ["trl", "alignprop"]

    def __init__(
        self,
        config: AlignPropConfig,
        reward_function: Callable[[torch.Tensor, Tuple[str], Tuple[Any]], torch.Tensor],
        prompt_function: Callable[[], Tuple[str, Any]],
        sd_pipeline: DDPOStableDiffusionPipeline,
        image_samples_hook: Optional[Callable[[Any, Any, Any], Any]] = None,
    ):
        if image_samples_hook is None:
            warn("No image_samples_hook provided; no images will be logged")

        self.prompt_fn = prompt_function
        self.reward_fn = reward_function
        self.config = config
        self.image_samples_callback = image_samples_hook

        accelerator_project_config = ProjectConfiguration(**self.config.project_kwargs)

        if self.config.resume_from:
            self.config.resume_from = os.path.normpath(os.path.expanduser(self.config.resume_from))
            if "checkpoint_" not in os.path.basename(self.config.resume_from):
                # get the most recent checkpoint in this directory
                checkpoints = list(
                    filter(
                        lambda x: "checkpoint_" in x,
                        os.listdir(self.config.resume_from),
                    )
                )
                if len(checkpoints) == 0:
                    raise ValueError(f"No checkpoints found in {self.config.resume_from}")
                checkpoint_numbers = sorted([int(x.split("_")[-1]) for x in checkpoints])
                self.config.resume_from = os.path.join(
                    self.config.resume_from,
                    f"checkpoint_{checkpoint_numbers[-1]}",
                )

                accelerator_project_config.iteration = checkpoint_numbers[-1] + 1

        self.accelerator = Accelerator(
            log_with=self.config.log_with,
            mixed_precision=self.config.mixed_precision,
            project_config=accelerator_project_config,
            # we always accumulate gradients across timesteps; we want config.train.gradient_accumulation_steps to be the
            # number of *samples* we accumulate across, so we need to multiply by the number of training timesteps to get
            # the total number of optimizer steps to accumulate across.
            gradient_accumulation_steps=self.config.train_gradient_accumulation_steps,
            **self.config.accelerator_kwargs,
        )

        is_using_tensorboard = config.log_with is not None and config.log_with == "tensorboard"

        if self.accelerator.is_main_process:
            self.accelerator.init_trackers(
                self.config.tracker_project_name,
                config=dict(alignprop_trainer_config=config.to_dict())
                if not is_using_tensorboard
                else config.to_dict(),
                init_kwargs=self.config.tracker_kwargs,
            )

        logger.info(f"\n{config}")

        set_seed(self.config.seed, device_specific=True)

        self.sd_pipeline = sd_pipeline

        self.sd_pipeline.set_progress_bar_config(
            position=1,
            disable=not self.accelerator.is_local_main_process,
            leave=False,
            desc="Timestep",
            dynamic_ncols=True,
        )

        # For mixed precision training we cast all non-trainable weights (vae, non-lora text_encoder and non-lora unet) to half-precision
        # as these weights are only used for inference, keeping weights in full precision is not required.
        if self.accelerator.mixed_precision == "fp16":
            inference_dtype = torch.float16
        elif self.accelerator.mixed_precision == "bf16":
            inference_dtype = torch.bfloat16
        else:
            inference_dtype = torch.float32

        self.sd_pipeline.vae.to(self.accelerator.device, dtype=inference_dtype)
        self.sd_pipeline.text_encoder.to(self.accelerator.device, dtype=inference_dtype)
        self.sd_pipeline.unet.to(self.accelerator.device, dtype=inference_dtype)

        trainable_layers = self.sd_pipeline.get_trainable_layers()

        self.accelerator.register_save_state_pre_hook(self._save_model_hook)
        self.accelerator.register_load_state_pre_hook(self._load_model_hook)

        # Enable TF32 for faster training on Ampere GPUs,
        # cf https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices
        if self.config.allow_tf32:
            torch.backends.cuda.matmul.allow_tf32 = True

        self.optimizer = self._setup_optimizer(
            trainable_layers.parameters() if not isinstance(trainable_layers, list) else trainable_layers
        )

        self.neg_prompt_embed = self.sd_pipeline.text_encoder(
            self.sd_pipeline.tokenizer(
                [""] if self.config.negative_prompts is None else self.config.negative_prompts,
                return_tensors="pt",
                padding="max_length",
                truncation=True,
                max_length=self.sd_pipeline.tokenizer.model_max_length,
            ).input_ids.to(self.accelerator.device)
        )[0]

        # NOTE: for some reason, autocast is necessary for non-lora training but for lora training it isn't necessary and it uses
        # more memory
        self.autocast = self.sd_pipeline.autocast or self.accelerator.autocast

        if hasattr(self.sd_pipeline, "use_lora") and self.sd_pipeline.use_lora:
            unet, self.optimizer = self.accelerator.prepare(trainable_layers, self.optimizer)
            self.trainable_layers = list(filter(lambda p: p.requires_grad, unet.parameters()))
        else:
            self.trainable_layers, self.optimizer = self.accelerator.prepare(trainable_layers, self.optimizer)

        if config.resume_from:
            logger.info(f"Resuming from {config.resume_from}")
            self.accelerator.load_state(config.resume_from)
            self.first_epoch = int(config.resume_from.split("_")[-1]) + 1
        else:
            self.first_epoch = 0

    def compute_rewards(self, prompt_image_pairs):
        reward, reward_metadata = self.reward_fn(
            prompt_image_pairs["images"], prompt_image_pairs["prompts"], prompt_image_pairs["prompt_metadata"]
        )
        return reward

    def step(self, epoch: int, global_step: int):
        """
        Perform a single step of training.

        Args:
            epoch (int): The current epoch.
            global_step (int): The current global step.

        Side Effects:
            - Model weights are updated
            - Logs the statistics to the accelerator trackers.
            - If `self.image_samples_callback` is not None, it will be called with the prompt_image_pairs, global_step, and the accelerator tracker.

        Returns:
            global_step (int): The updated global step.

        """
        info = defaultdict(list)

        self.sd_pipeline.unet.train()

        for _ in range(self.config.train_gradient_accumulation_steps):
            with self.accelerator.accumulate(self.sd_pipeline.unet), self.autocast(), torch.enable_grad():
                prompt_image_pairs = self._generate_samples(
                    batch_size=self.config.train_batch_size,
                )

                rewards = self.compute_rewards(prompt_image_pairs)

                prompt_image_pairs["rewards"] = rewards

                rewards_vis = self.accelerator.gather(rewards).detach().cpu().numpy()

                loss = self.calculate_loss(rewards)

                self.accelerator.backward(loss)

                if self.accelerator.sync_gradients:
                    self.accelerator.clip_grad_norm_(
                        self.trainable_layers.parameters()
                        if not isinstance(self.trainable_layers, list)
                        else self.trainable_layers,
                        self.config.train_max_grad_norm,
                    )

                self.optimizer.step()
                self.optimizer.zero_grad()

            info["reward_mean"].append(rewards_vis.mean())
            info["reward_std"].append(rewards_vis.std())
            info["loss"].append(loss.item())

        # Checks if the accelerator has performed an optimization step behind the scenes
        if self.accelerator.sync_gradients:
            # log training-related stuff
            info = {k: torch.mean(torch.tensor(v)) for k, v in info.items()}
            info = self.accelerator.reduce(info, reduction="mean")
            info.update({"epoch": epoch})
            self.accelerator.log(info, step=global_step)
            global_step += 1
            info = defaultdict(list)
        else:
            raise ValueError(
                "Optimization step should have been performed by this point. Please check calculated gradient accumulation settings."
            )
        # Logs generated images
        if self.image_samples_callback is not None and global_step % self.config.log_image_freq == 0:
            self.image_samples_callback(prompt_image_pairs, global_step, self.accelerator.trackers[0])

        if epoch != 0 and epoch % self.config.save_freq == 0 and self.accelerator.is_main_process:
            self.accelerator.save_state()

        return global_step

    def calculate_loss(self, rewards):
        """
        Calculate the loss for a batch of an unpacked sample

        Args:
            rewards (torch.Tensor):
                Differentiable reward scalars for each generated image, shape: [batch_size]
        Returns:
            loss (torch.Tensor)
            (all of these are of shape (1,))
        """
        #  Loss is specific to Aesthetic Reward function used in AlignProp (https://arxiv.org/pdf/2310.03739.pdf)
        loss = 10.0 - (rewards).mean()
        return loss

    def loss(
        self,
        advantages: torch.Tensor,
        clip_range: float,
        ratio: torch.Tensor,
    ):
        unclipped_loss = -advantages * ratio
        clipped_loss = -advantages * torch.clamp(
            ratio,
            1.0 - clip_range,
            1.0 + clip_range,
        )
        return torch.mean(torch.maximum(unclipped_loss, clipped_loss))

    def _setup_optimizer(self, trainable_layers_parameters):
        if self.config.train_use_8bit_adam:
            import bitsandbytes

            optimizer_cls = bitsandbytes.optim.AdamW8bit
        else:
            optimizer_cls = torch.optim.AdamW

        return optimizer_cls(
            trainable_layers_parameters,
            lr=self.config.train_learning_rate,
            betas=(self.config.train_adam_beta1, self.config.train_adam_beta2),
            weight_decay=self.config.train_adam_weight_decay,
            eps=self.config.train_adam_epsilon,
        )

    def _save_model_hook(self, models, weights, output_dir):
        self.sd_pipeline.save_checkpoint(models, weights, output_dir)
        weights.pop()  # ensures that accelerate doesn't try to handle saving of the model

    def _load_model_hook(self, models, input_dir):
        self.sd_pipeline.load_checkpoint(models, input_dir)
        models.pop()  # ensures that accelerate doesn't try to handle loading of the model

    def _generate_samples(self, batch_size, with_grad=True, prompts=None):
        """
        Generate samples from the model

        Args:
            batch_size (int): Batch size to use for sampling
            with_grad (bool): Whether the generated RGBs should have gradients attached to it.

        Returns:
            prompt_image_pairs (Dict[Any])
        """
        prompt_image_pairs = {}

        sample_neg_prompt_embeds = self.neg_prompt_embed.repeat(batch_size, 1, 1)

        if prompts is None:
            prompts, prompt_metadata = zip(*[self.prompt_fn() for _ in range(batch_size)])
        else:
            prompt_metadata = [{} for _ in range(batch_size)]

        prompt_ids = self.sd_pipeline.tokenizer(
            prompts,
            return_tensors="pt",
            padding="max_length",
            truncation=True,
            max_length=self.sd_pipeline.tokenizer.model_max_length,
        ).input_ids.to(self.accelerator.device)

        prompt_embeds = self.sd_pipeline.text_encoder(prompt_ids)[0]

        if with_grad:
            sd_output = self.sd_pipeline.rgb_with_grad(
                prompt_embeds=prompt_embeds,
                negative_prompt_embeds=sample_neg_prompt_embeds,
                num_inference_steps=self.config.sample_num_steps,
                guidance_scale=self.config.sample_guidance_scale,
                eta=self.config.sample_eta,
                truncated_backprop_rand=self.config.truncated_backprop_rand,
                truncated_backprop_timestep=self.config.truncated_backprop_timestep,
                truncated_rand_backprop_minmax=self.config.truncated_rand_backprop_minmax,
                output_type="pt",
            )
        else:
            sd_output = self.sd_pipeline(
                prompt_embeds=prompt_embeds,
                negative_prompt_embeds=sample_neg_prompt_embeds,
                num_inference_steps=self.config.sample_num_steps,
                guidance_scale=self.config.sample_guidance_scale,
                eta=self.config.sample_eta,
                output_type="pt",
            )

        images = sd_output.images

        prompt_image_pairs["images"] = images
        prompt_image_pairs["prompts"] = prompts
        prompt_image_pairs["prompt_metadata"] = prompt_metadata

        return prompt_image_pairs

    def train(self, epochs: Optional[int] = None):
        """
        Train the model for a given number of epochs
        """
        global_step = 0
        if epochs is None:
            epochs = self.config.num_epochs
        for epoch in range(self.first_epoch, epochs):
            global_step = self.step(epoch, global_step)

    def create_model_card(self, path: str, model_name: Optional[str] = "TRL AlignProp Model") -> None:
        """Creates and saves a model card for a TRL model.

        Args:
            path (`str`): The path to save the model card to.
            model_name (`str`, *optional*): The name of the model, defaults to `TRL AlignProp Model`.
        """
        try:
            user = whoami()["name"]
        # handle the offline case
        except Exception:
            warnings.warn("Cannot retrieve user information assuming you are running in offline mode.")
            return

        if not os.path.exists(path):
            os.makedirs(path)

        model_card_content = MODEL_CARD_TEMPLATE.format(model_name=model_name, model_id=f"{user}/{path}")
        with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as f:
            f.write(model_card_content)

    def _save_pretrained(self, save_directory):
        self.sd_pipeline.save_pretrained(save_directory)
        self.create_model_card(save_directory)
class UnslothAlignPropTrainer(_UnslothAlignPropTrainer):
    """
    
    The AlignPropTrainer uses Deep Diffusion Policy Optimization to optimise diffusion models.
    Note, this trainer is heavily inspired by the work here: https://github.com/mihirp1998/AlignProp/
    As of now only Stable Diffusion based pipelines are supported

    Attributes:
        **config** (`AlignPropConfig`) -- Configuration object for AlignPropTrainer. Check the documentation of `PPOConfig` for more
         details.
        **reward_function** (Callable[[torch.Tensor, Tuple[str], Tuple[Any]], torch.Tensor]) -- Reward function to be used
        **prompt_function** (Callable[[], Tuple[str, Any]]) -- Function to generate prompts to guide model
        **sd_pipeline** (`DDPOStableDiffusionPipeline`) -- Stable Diffusion pipeline to be used for training.
        **image_samples_hook** (Optional[Callable[[Any, Any, Any], Any]]) -- Hook to be called to log images
    
    """
    def __init__(
        self,
        config,
        reward_function,
        prompt_function,
        sd_pipeline,
        image_samples_hook = None,
        **kwargs
    ):
        if args is None: args = UnslothAlignPropConfig()
        other_metrics = []
        
        from unsloth_zoo.logging_utils import PatchRLStatistics
        PatchRLStatistics('alignprop_trainer', other_metrics)
        
        super().__init__(
            config = config,
            reward_function = reward_function,
            prompt_function = prompt_function,
            sd_pipeline = sd_pipeline,
            image_samples_hook = image_samples_hook,**kwargs)
        
pass
