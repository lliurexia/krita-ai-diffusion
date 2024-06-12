from __future__ import annotations

from copy import copy
from dataclasses import dataclass, field
from typing import Any
import math
import random

from . import resolution, resources
from .api import ControlInput, ImageInput, CheckpointInput, SamplingInput, WorkflowInput, LoraInput
from .api import ExtentInput, InpaintMode, InpaintParams, FillMode, ConditioningInput, WorkflowKind
from .api import RegionInput
from .image import Bounds, Extent, Image, Mask, Point, multiple_of
from .client import ClientModels, ModelDict
from .style import Style, StyleSettings, SamplerPresets
from .resolution import ScaledExtent, ScaleMode, TileLayout, get_inpaint_reference
from .resources import ControlMode, SDVersion, UpscalerName, ResourceKind
from .settings import PerformanceSettings
from .text import merge_prompt, extract_loras
from .comfy_workflow import ComfyWorkflow, ComfyRunMode, Output
from .util import ensure, median_or_zero, unique, client_logger as log


def detect_inpaint_mode(extent: Extent, area: Bounds):
    if area.width >= extent.width or area.height >= extent.height:
        return InpaintMode.expand
    return InpaintMode.fill


def generate_seed():
    # Currently only using 32 bit because Qt widgets don't support int64
    return random.randint(0, 2**31 - 1)


def _sampling_from_style(style: Style, strength: float, is_live: bool):
    sampler_name = style.live_sampler if is_live else style.sampler
    cfg = style.live_cfg_scale if is_live else style.cfg_scale
    total_steps = style.live_sampler_steps if is_live else style.sampler_steps
    preset = SamplerPresets.instance()[sampler_name]
    result = SamplingInput(
        sampler=preset.sampler,
        scheduler=preset.scheduler,
        cfg_scale=cfg or preset.cfg,
        total_steps=total_steps or preset.steps,
    )
    if strength < 1.0:
        min_steps = min(preset.minimum_steps, total_steps)
        result.total_steps, result.start_step = _apply_strength(strength, total_steps, min_steps)
    return result


def _apply_strength(strength: float, steps: int, min_steps: int = 0) -> tuple[int, int]:
    start_at_step = round(steps * (1 - strength))

    if min_steps and steps - start_at_step < min_steps:
        steps = math.floor(min_steps / strength)
        start_at_step = steps - min_steps

    return steps, start_at_step


def _sampler_params(sampling: SamplingInput, strength: float | None = None, advanced=True):
    """Assemble the parameters which are passed to ComfyUI's KSampler/KSamplerAdvanced node.
    Optionally adjust the number of steps based on the strength parameter (for hires pass).
    """
    params: dict[str, Any] = dict(
        sampler=sampling.sampler,
        scheduler=sampling.scheduler,
        steps=sampling.actual_steps,
        cfg=sampling.cfg_scale,
        seed=sampling.seed,
    )
    assert strength is None or advanced
    if advanced:
        params["steps"] = sampling.total_steps
        params["start_at_step"] = sampling.start_step
        if strength is not None:
            params["steps"], params["start_at_step"] = _apply_strength(
                strength, sampling.total_steps
            )
    return params


def load_checkpoint_with_lora(w: ComfyWorkflow, checkpoint: CheckpointInput, models: ClientModels):
    checkpoint_model = checkpoint.checkpoint
    if checkpoint_model not in models.checkpoints:
        checkpoint_model = next(iter(models.checkpoints.keys()))
        log.warning(
            f"Style checkpoint {checkpoint.checkpoint} not found, using default {checkpoint_model}"
        )
    model, clip, vae = w.load_checkpoint(checkpoint_model)

    if checkpoint.version is SDVersion.sd3:
        clip_models = models.for_version(checkpoint.version).clip
        model = w.model_sampling_sd3(model)
        clip = w.load_dual_clip(clip_models["clip_g"], clip_models["clip_l"], type="sd3")

    if checkpoint.clip_skip != StyleSettings.clip_skip.default:
        clip = w.clip_set_last_layer(clip, (checkpoint.clip_skip * -1))

    if checkpoint.vae != StyleSettings.vae.default:
        if checkpoint.vae in models.vae:
            vae = w.load_vae(checkpoint.vae)
        else:
            log.warning(f"Style VAE {checkpoint.vae} not found, using default VAE from checkpoint")

    for lora in checkpoint.loras:
        model, clip = w.load_lora(model, clip, lora.name, lora.strength, lora.strength)

    if checkpoint.v_prediction_zsnr:
        model = w.model_sampling_discrete(model, "v_prediction", zsnr=True)
        model = w.rescale_cfg(model, 0.7)

    if checkpoint.version.supports_lcm:
        lcm_lora = models.for_checkpoint(checkpoint_model).lora["lcm"]
        if any(l.name == lcm_lora for l in checkpoint.loras):
            model = w.model_sampling_discrete(model, "lcm")

    if checkpoint.self_attention_guidance:
        model = w.apply_self_attention_guidance(model)

    return model, clip, vae


class ImageOutput:
    image: Image | None = None
    is_mask: bool = False
    _output: Output | None = None
    _scaled: Output | None = None
    _scale: Extent | None = None

    def __init__(self, image: Image | Output | None, is_mask: bool = False):
        if isinstance(image, Output):
            self._output = image
        else:
            self.image = image
        self.is_mask = is_mask

    def load(
        self,
        w: ComfyWorkflow,
        target_extent: Extent | None = None,
        default_image: Output | None = None,
    ):
        if self._output is None:
            if self.image is None:
                self._output = ensure(default_image)
                if target_extent is not None:
                    self._output = w.scale_image(self._output, target_extent)
            elif self.is_mask:
                self._output = w.load_mask(self.image)
            else:
                self._output = w.load_image(self.image)
        if target_extent is None or self.image is None or self.image.extent == target_extent:
            return self._output

        if self._scaled is None or self._scale != target_extent:
            if self.is_mask:
                self._scaled = w.scale_mask(self._output, target_extent)
            else:
                self._scaled = w.scale_control_image(self._output, target_extent)
            self._scale = target_extent
        return self._scaled

    def crop(self, bounds: Bounds):
        if self.image is not None:
            self.image = Image.crop(self.image, bounds)
            self._output = None
            self._scale = None

    def __bool__(self):
        return self.image is not None


@dataclass
class Control:
    mode: ControlMode
    image: ImageOutput
    mask: ImageOutput | None = None
    strength: float = 1.0
    range: tuple[float, float] = (0.0, 1.0)

    @staticmethod
    def from_input(i: ControlInput):
        return Control(i.mode, ImageOutput(i.image), None, i.strength, i.range)


class TextPrompt:
    text: str
    _output: Output | None = None

    def __init__(self, text: str):
        self.text = text

    def encode(self, w: ComfyWorkflow, clip: Output, style_prompt: str | None = None):
        text = self.text
        if text != "" and style_prompt:
            text = merge_prompt(text, style_prompt)
        if self._output is None:
            self._output = w.clip_text_encode(clip, text)
        return self._output


@dataclass
class Region:
    mask: ImageOutput
    bounds: Bounds
    positive: TextPrompt
    control: list[Control] = field(default_factory=list)
    is_background: bool = False

    @staticmethod
    def from_input(i: RegionInput, index: int):
        control = [Control.from_input(c) for c in i.control]
        mask = ImageOutput(i.mask, is_mask=True)
        return Region(mask, i.bounds, TextPrompt(i.positive), control, is_background=index == 0)

    def copy(self):
        control = [copy(c) for c in self.control]
        return Region(self.mask, self.bounds, self.positive, control, self.is_background)


@dataclass
class Conditioning:
    positive: TextPrompt
    negative: TextPrompt
    control: list[Control] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    style_prompt: str = ""

    @staticmethod
    def from_input(i: ConditioningInput):
        return Conditioning(
            TextPrompt(i.positive),
            TextPrompt(i.negative),
            [Control.from_input(c) for c in i.control],
            [Region.from_input(r, i) for i, r in enumerate(i.regions)],
            i.style,
        )

    def copy(self):
        return Conditioning(
            self.positive,
            self.negative,
            [copy(c) for c in self.control],
            [r.copy() for r in self.regions],
            self.style_prompt,
        )

    def downscale(self, original: Extent, target: Extent):
        downscale_control_images(self.all_control, original, target)

    def crop(self, bounds: Bounds):
        # Meant to be called during preperation, before adding inpaint layer.
        for control in self.all_control:
            assert control.mask is None
            control.image.crop(bounds)
        for region in self.regions:
            region.mask.crop(bounds)

    @property
    def all_control(self):
        return self.control + [c for r in self.regions for c in r.control]


def downscale_control_images(
    control_layers: list[Control] | list[ControlInput], original: Extent, target: Extent
):
    # Meant to be called during preperation, when desired generation resolution is lower than canvas size:
    # no need to encode and send the full resolution images to the server.
    if original.width > target.width and original.height > target.height:
        for control in control_layers:
            assert isinstance(control.image, Image)
            # Only scale if control image resolution matches canvas resolution
            if control.image.extent == original and control.mode is not ControlMode.canny_edge:
                if isinstance(control, ControlInput):
                    control.image = Image.scale(control.image, target)
                elif control.image.image:
                    control.image.image = Image.scale(control.image.image, target)


def downscale_all_control_images(cond: ConditioningInput, original: Extent, target: Extent):
    downscale_control_images(cond.control, original, target)
    for region in cond.regions:
        downscale_control_images(region.control, original, target)


def encode_text_prompt(w: ComfyWorkflow, cond: Conditioning, clip: Output):
    positive = cond.positive.encode(w, clip, cond.style_prompt)
    negative = cond.negative.encode(w, clip)
    return positive, negative


def apply_attention_mask(
    w: ComfyWorkflow, model: Output, cond: Conditioning, clip: Output, target_extent: Extent | None
):
    if len(cond.regions) == 0:
        return model

    if len(cond.regions) == 1:
        region = cond.regions[0]
        cond.positive = region.positive
        cond.control += region.control
        return model

    bottom_region = cond.regions[0]
    if bottom_region.is_background:
        regions = w.background_region(bottom_region.positive.encode(w, clip, cond.style_prompt))
        remaining = cond.regions[1:]
    else:
        regions = w.background_region(cond.positive.encode(w, clip, cond.style_prompt))
        remaining = cond.regions

    for region in remaining:
        mask = region.mask.load(w, target_extent)
        prompt = region.positive.encode(w, clip, cond.style_prompt)
        regions = w.define_region(regions, mask, prompt)

    return w.attention_mask(model, regions)


def apply_control(
    w: ComfyWorkflow,
    positive: Output,
    negative: Output,
    control_layers: list[Control],
    extent: Extent | None,
    models: ModelDict,
):
    models = models.control
    for control in (c for c in control_layers if c.mode.is_control_net):
        image = control.image.load(w, extent)
        if control.mode is ControlMode.inpaint:
            assert control.mask is not None, "Inpaint control requires a mask"
            image = w.inpaint_preprocessor(image, control.mask.load(w))
        if control.mode.is_lines:  # ControlNet expects white lines on black background
            image = w.invert_image(image)
        controlnet = w.load_controlnet(models[control.mode])
        positive, negative = w.apply_controlnet(
            positive,
            negative,
            controlnet,
            image,
            strength=control.strength,
            range=control.range,
        )

    return positive, negative


def apply_ip_adapter(
    w: ComfyWorkflow,
    model: Output,
    control_layers: list[Control],
    models: ModelDict,
    mask: Output | None = None,
):
    models = models.ip_adapter

    # Create a separate embedding for each face ID (though more than 1 is questionable)
    face_layers = [c for c in control_layers if c.mode is ControlMode.face]
    if len(face_layers) > 0:
        clip_vision = w.load_clip_vision(models.clip_vision)
        ip_adapter = w.load_ip_adapter(models[ControlMode.face])
        insight_face = w.load_insight_face()
        for control in face_layers:
            model = w.apply_ip_adapter_face(
                model,
                ip_adapter,
                clip_vision,
                insight_face,
                control.image.load(w),
                control.strength,
                range=control.range,
                mask=mask,
            )

    # Encode images with their weights into a batch and apply IP-adapter to the model once
    def encode_and_apply_ip_adapter(model: Output, control_layers: list[Control], weight_type: str):
        clip_vision = w.load_clip_vision(models.clip_vision)
        ip_adapter = w.load_ip_adapter(models[ControlMode.reference])
        embeds: list[Output] = []
        range = (0.99, 0.01)

        for control in control_layers:
            if len(embeds) >= 5:
                raise Exception(f"Too many control layers of type '{mode.text}' (maximum is 5)")
            img = control.image.load(w)
            embeds.append(w.encode_ip_adapter(img, control.strength, ip_adapter, clip_vision)[0])
            range = (min(range[0], control.range[0]), max(range[1], control.range[1]))

        combined = w.combine_ip_adapter_embeds(embeds) if len(embeds) > 1 else embeds[0]
        return w.apply_ip_adapter(
            model, ip_adapter, clip_vision, combined, 1.0, weight_type, range, mask
        )

    modes = [
        (ControlMode.reference, "linear"),
        (ControlMode.style, "style transfer"),
        (ControlMode.composition, "composition"),
    ]
    # Chain together different IP-adapter weight types.
    for mode, weight_type in modes:
        ref_layers = [c for c in control_layers if c.mode is mode]
        if len(ref_layers) > 0:
            model = encode_and_apply_ip_adapter(model, ref_layers, weight_type)

    return model


def apply_regional_ip_adapter(
    w: ComfyWorkflow, model: Output, regions: list[Region], extent: Extent | None, models: ModelDict
):
    for region in (r for r in regions if r.mask):
        model = apply_ip_adapter(w, model, region.control, models, region.mask.load(w, extent))
    return model


def scale(
    extent: Extent,
    target: Extent,
    mode: ScaleMode,
    w: ComfyWorkflow,
    image: Output,
    models: ModelDict,
):
    """Handles scaling images from `extent` to `target` resolution.
    Uses either lanczos or a fast upscaling model."""

    if mode is ScaleMode.none:
        return image
    elif mode is ScaleMode.resize:
        return w.scale_image(image, target)
    else:
        assert mode is ScaleMode.upscale_fast
        ratio = target.pixel_count / extent.pixel_count
        factor = max(2, min(4, math.ceil(math.sqrt(ratio))))
        upscale_model = w.load_upscale_model(models.upscale[UpscalerName.fast_x(factor)])
        image = w.upscale_image(upscale_model, image)
        image = w.scale_image(image, target)
        return image


def scale_to_initial(
    extent: ScaledExtent, w: ComfyWorkflow, image: Output, models: ModelDict, is_mask=False
):
    if is_mask and extent.initial_scaling is ScaleMode.resize:
        return w.scale_mask(image, extent.initial)
    elif not is_mask:
        return scale(extent.input, extent.initial, extent.initial_scaling, w, image, models)
    else:
        assert is_mask and extent.initial_scaling is ScaleMode.none
        return image


def scale_to_target(extent: ScaledExtent, w: ComfyWorkflow, image: Output, models: ModelDict):
    return scale(extent.desired, extent.target, extent.target_scaling, w, image, models)


def scale_refine_and_decode(
    extent: ScaledExtent,
    w: ComfyWorkflow,
    cond: Conditioning,
    sampling: SamplingInput,
    latent: Output,
    prompt_pos: Output,
    prompt_neg: Output,
    model: Output,
    clip: Output,
    vae: Output,
    models: ModelDict,
):
    """Handles scaling images from `initial` to `desired` resolution.
    If it is a substantial upscale, runs a high-res SD refinement pass.
    Takes latent as input and returns a decoded image."""

    mode = extent.refinement_scaling
    if mode in [ScaleMode.none, ScaleMode.resize, ScaleMode.upscale_fast]:
        decoded = w.vae_decode(vae, latent)
        return scale(extent.initial, extent.desired, mode, w, decoded, models)

    model = apply_attention_mask(w, model, cond, clip, extent.desired)
    model = apply_regional_ip_adapter(w, model, cond.regions, extent.desired, models)

    if mode is ScaleMode.upscale_small:
        upscaler = models.upscale[UpscalerName.fast_2x]
    else:
        assert mode is ScaleMode.upscale_quality
        upscaler = models.upscale[UpscalerName.default]

    upscale_model = w.load_upscale_model(upscaler)
    decoded = w.vae_decode(vae, latent)
    upscale = w.upscale_image(upscale_model, decoded)
    upscale = w.scale_image(upscale, extent.desired)
    latent = w.vae_encode(vae, upscale)
    params = _sampler_params(sampling, strength=0.4)

    positive, negative = apply_control(
        w, prompt_pos, prompt_neg, cond.all_control, extent.desired, models
    )
    result = w.ksampler_advanced(model, positive, negative, latent, **params)
    image = w.vae_decode(vae, result)
    return image


def generate(
    w: ComfyWorkflow,
    checkpoint: CheckpointInput,
    extent: ScaledExtent,
    cond: Conditioning,
    sampling: SamplingInput,
    batch_count: int,
    models: ModelDict,
):
    model, clip, vae = load_checkpoint_with_lora(w, checkpoint, models.all)
    model = apply_ip_adapter(w, model, cond.control, models)
    model_orig = copy(model)
    model = apply_attention_mask(w, model, cond, clip, extent.initial)
    model = apply_regional_ip_adapter(w, model, cond.regions, extent.initial, models)
    latent = w.empty_latent_image(extent.initial, models.version, batch_count)
    prompt_pos, prompt_neg = encode_text_prompt(w, cond, clip)
    positive, negative = apply_control(
        w, prompt_pos, prompt_neg, cond.all_control, extent.initial, models
    )
    out_latent = w.ksampler_advanced(model, positive, negative, latent, **_sampler_params(sampling))
    out_image = scale_refine_and_decode(
        extent, w, cond, sampling, out_latent, prompt_pos, prompt_neg, model_orig, clip, vae, models
    )
    out_image = scale_to_target(extent, w, out_image, models)
    w.send_image(out_image)
    return w


def fill_masked(w: ComfyWorkflow, image: Output, mask: Output, fill: FillMode, models: ModelDict):
    if fill is FillMode.blur:
        return w.blur_masked(image, mask, 65, falloff=9)
    elif fill is FillMode.border:
        image = w.fill_masked(image, mask, "navier-stokes")
        return w.blur_masked(image, mask, 65)
    elif fill is FillMode.neutral:
        return w.fill_masked(image, mask, "neutral", falloff=9)
    elif fill is FillMode.inpaint:
        model = w.load_inpaint_model(models.inpaint["default"])
        return w.inpaint_image(model, image, mask)
    elif fill is FillMode.replace:
        return w.fill_masked(image, mask, "neutral")
    return image


def detect_inpaint(
    mode: InpaintMode,
    bounds: Bounds,
    sd_ver: SDVersion,
    prompt: str,
    control: list[ControlInput],
    strength: float,
):
    assert mode is not InpaintMode.automatic
    result = InpaintParams(mode, bounds)

    if sd_ver is SDVersion.sd15:
        result.use_inpaint_model = strength > 0.5
        result.use_condition_mask = (
            mode is InpaintMode.add_object
            and prompt != ""
            and not any(c.mode.is_structural for c in control)
        )
    elif sd_ver is SDVersion.sdxl:
        result.use_inpaint_model = strength > 0.8

    is_ref_mode = mode in [InpaintMode.fill, InpaintMode.expand]
    result.use_reference = is_ref_mode and prompt == ""

    result.fill = {
        InpaintMode.fill: FillMode.blur,
        InpaintMode.expand: FillMode.border,
        InpaintMode.add_object: FillMode.neutral,
        InpaintMode.remove_object: FillMode.inpaint,
        InpaintMode.replace_background: FillMode.replace,
    }[mode]
    return result


def inpaint(
    w: ComfyWorkflow,
    images: ImageInput,
    checkpoint: CheckpointInput,
    cond: Conditioning,
    sampling: SamplingInput,
    params: InpaintParams,
    crop_upscale_extent: Extent,
    batch_count: int,
    models: ModelDict,
):
    target_bounds = params.target_bounds
    extent = ScaledExtent.from_input(images.extent)  # for initial generation with large context

    model, clip, vae = load_checkpoint_with_lora(w, checkpoint, models.all)
    model = w.differential_diffusion(model)
    model_orig = copy(model)

    upscale_extent = ScaledExtent(  # after crop to the masked region
        Extent(0, 0), Extent(0, 0), crop_upscale_extent, target_bounds.extent
    )
    initial_bounds = extent.convert(target_bounds, "target", "initial")

    in_image = w.load_image(ensure(images.initial_image))
    in_image = scale_to_initial(extent, w, in_image, models)
    in_mask = w.load_mask(ensure(images.initial_mask))
    in_mask = scale_to_initial(extent, w, in_mask, models, is_mask=True)
    cropped_mask = w.load_mask(ensure(images.hires_mask))

    cond_base = cond.copy()
    cond_base.downscale(extent.input, extent.initial)
    model = apply_attention_mask(w, model, cond_base, clip, extent.initial)

    if params.use_reference:
        reference = get_inpaint_reference(ensure(images.initial_image), initial_bounds) or in_image
        cond_base.control.append(
            Control(ControlMode.reference, ImageOutput(reference), None, 0.5, (0.2, 0.8))
        )
    if params.use_inpaint_model and models.version is SDVersion.sd15:
        cond_base.control.append(
            Control(ControlMode.inpaint, ImageOutput(in_image), ImageOutput(in_mask, is_mask=True))
        )
    if params.use_condition_mask and len(cond_base.regions) == 0:
        focus_mask = ImageOutput(in_mask, is_mask=True)
        base_prompt = TextPrompt(merge_prompt("", cond_base.style_prompt))
        cond_base.regions = [
            Region(ImageOutput(None), Bounds(0, 0, *extent.initial), base_prompt, []),
            Region(focus_mask, initial_bounds, cond_base.positive, []),
        ]
    in_image = fill_masked(w, in_image, in_mask, params.fill, models)

    model = apply_ip_adapter(w, model, cond_base.control, models)
    model = apply_regional_ip_adapter(w, model, cond_base.regions, extent.initial, models)
    positive, negative = encode_text_prompt(w, cond, clip)
    positive, negative = apply_control(
        w, positive, negative, cond_base.all_control, extent.initial, models
    )
    if params.use_inpaint_model and models.version is SDVersion.sdxl:
        positive, negative, latent_inpaint, latent = w.vae_encode_inpaint_conditioning(
            vae, in_image, in_mask, positive, negative
        )
        inpaint_patch = w.load_fooocus_inpaint(**models.fooocus_inpaint)
        inpaint_model = w.apply_fooocus_inpaint(model, inpaint_patch, latent_inpaint)
    else:
        latent = w.vae_encode(vae, in_image)
        latent = w.set_latent_noise_mask(latent, in_mask)
        inpaint_model = model

    latent = w.batch_latent(latent, batch_count)
    out_latent = w.ksampler_advanced(
        inpaint_model, positive, negative, latent, **_sampler_params(sampling)
    )

    if extent.refinement_scaling in [ScaleMode.upscale_small, ScaleMode.upscale_quality]:
        model = model_orig
        if extent.refinement_scaling is ScaleMode.upscale_small:
            upscaler = models.upscale[UpscalerName.fast_2x]
        else:
            upscaler = models.upscale[UpscalerName.default]
        sampler_params = _sampler_params(sampling, strength=0.4)
        upscale_model = w.load_upscale_model(upscaler)
        upscale = w.vae_decode(vae, out_latent)
        upscale = w.crop_image(upscale, initial_bounds)
        upscale = w.upscale_image(upscale_model, upscale)
        upscale = w.scale_image(upscale, upscale_extent.desired)
        latent = w.vae_encode(vae, upscale)
        latent = w.set_latent_noise_mask(latent, cropped_mask)

        cond_upscale = cond.copy()
        cond_upscale.crop(target_bounds)
        res = upscale_extent.desired

        positive_up, negative_up = encode_text_prompt(w, cond_upscale, clip)
        model = apply_attention_mask(w, model, cond_upscale, clip, res)
        model = apply_regional_ip_adapter(w, model, cond_upscale.regions, res, models)

        if params.use_inpaint_model and models.version is SDVersion.sd15:
            hires_image = ImageOutput(images.hires_image)
            hires_mask = ImageOutput(cropped_mask, is_mask=True)
            cond_upscale.control.append(Control(ControlMode.inpaint, hires_image, hires_mask))
        positive_up, negative_up = apply_control(
            w, positive_up, negative_up, cond_upscale.all_control, res, models
        )
        out_latent = w.ksampler_advanced(model, positive_up, negative_up, latent, **sampler_params)
        out_image = w.vae_decode(vae, out_latent)
        out_image = scale_to_target(upscale_extent, w, out_image, models)
    else:
        desired_bounds = extent.convert(target_bounds, "target", "desired")
        desired_extent = desired_bounds.extent
        cropped_extent = ScaledExtent(
            desired_extent, desired_extent, desired_extent, target_bounds.extent
        )
        out_image = w.vae_decode(vae, out_latent)
        out_image = scale(
            extent.initial, extent.desired, extent.refinement_scaling, w, out_image, models
        )
        out_image = w.crop_image(out_image, desired_bounds)
        out_image = scale_to_target(cropped_extent, w, out_image, models)

    compositing_mask = w.denoise_to_compositing_mask(cropped_mask)
    out_masked = w.apply_mask(out_image, compositing_mask)
    w.send_image(out_masked)
    return w


def refine(
    w: ComfyWorkflow,
    image: Image,
    extent: ScaledExtent,
    checkpoint: CheckpointInput,
    cond: Conditioning,
    sampling: SamplingInput,
    batch_count: int,
    models: ModelDict,
):
    model, clip, vae = load_checkpoint_with_lora(w, checkpoint, models.all)
    model = apply_ip_adapter(w, model, cond.control, models)
    model = apply_attention_mask(w, model, cond, clip, extent.initial)
    model = apply_regional_ip_adapter(w, model, cond.regions, extent.initial, models)
    in_image = w.load_image(image)
    in_image = scale_to_initial(extent, w, in_image, models)
    latent = w.vae_encode(vae, in_image)
    if batch_count > 1:
        latent = w.batch_latent(latent, batch_count)
    positive, negative = encode_text_prompt(w, cond, clip)
    positive, negative = apply_control(
        w, positive, negative, cond.all_control, extent.desired, models
    )
    sampler = w.ksampler_advanced(model, positive, negative, latent, **_sampler_params(sampling))
    out_image = w.vae_decode(vae, sampler)
    out_image = scale_to_target(extent, w, out_image, models)
    w.send_image(out_image)
    return w


def refine_region(
    w: ComfyWorkflow,
    images: ImageInput,
    checkpoint: CheckpointInput,
    cond: Conditioning,
    sampling: SamplingInput,
    inpaint: InpaintParams,
    batch_count: int,
    models: ModelDict,
):
    extent = ScaledExtent.from_input(images.extent)

    model, clip, vae = load_checkpoint_with_lora(w, checkpoint, models.all)
    model = w.differential_diffusion(model)
    model = apply_ip_adapter(w, model, cond.control, models)
    model_orig = copy(model)
    model = apply_attention_mask(w, model, cond, clip, extent.initial)
    model = apply_regional_ip_adapter(w, model, cond.regions, extent.initial, models)
    prompt_pos, prompt_neg = encode_text_prompt(w, cond, clip)

    in_image = w.load_image(ensure(images.initial_image))
    in_image = scale_to_initial(extent, w, in_image, models)
    in_mask = w.load_mask(ensure(images.initial_mask))
    in_mask = scale_to_initial(extent, w, in_mask, models, is_mask=True)

    if inpaint.use_inpaint_model and models.version is SDVersion.sd15:
        c_mask = ImageOutput(in_mask, is_mask=True)
        cond.control.append(Control(ControlMode.inpaint, ImageOutput(in_image), c_mask))
    positive, negative = apply_control(
        w, prompt_pos, prompt_neg, cond.all_control, extent.initial, models
    )
    if models.version is SDVersion.sd15 or not inpaint.use_inpaint_model:
        latent = w.vae_encode(vae, in_image)
        latent = w.set_latent_noise_mask(latent, in_mask)
        inpaint_model = model
    else:  # SDXL inpaint model
        positive, negative, latent_inpaint, latent = w.vae_encode_inpaint_conditioning(
            vae, in_image, in_mask, positive, negative
        )
        inpaint_patch = w.load_fooocus_inpaint(**models.fooocus_inpaint)
        inpaint_model = w.apply_fooocus_inpaint(model, inpaint_patch, latent_inpaint)

    if batch_count > 1:
        latent = w.batch_latent(latent, batch_count)

    out_latent = w.ksampler_advanced(
        inpaint_model, positive, negative, latent, **_sampler_params(sampling)
    )
    out_image = scale_refine_and_decode(
        extent, w, cond, sampling, out_latent, prompt_pos, prompt_neg, model_orig, clip, vae, models
    )
    out_image = scale_to_target(extent, w, out_image, models)
    if extent.target != inpaint.target_bounds.extent:
        out_image = w.crop_image(out_image, inpaint.target_bounds)
    original_mask = w.load_mask(ensure(images.hires_mask))
    compositing_mask = w.denoise_to_compositing_mask(original_mask)
    out_masked = w.apply_mask(out_image, compositing_mask)
    w.send_image(out_masked)
    return w


def create_control_image(
    w: ComfyWorkflow,
    image: Image,
    mode: ControlMode,
    extent: ScaledExtent,
    bounds: Bounds | None = None,
    seed: int = -1,
):
    assert mode not in [ControlMode.reference, ControlMode.face, ControlMode.inpaint]

    current_extent = extent.input
    input = w.load_image(image)
    result = None

    if mode is ControlMode.canny_edge:
        result = w.add("Canny", 1, image=input, low_threshold=0.4, high_threshold=0.8)

    elif mode is ControlMode.hands:
        if bounds is None:
            current_extent = current_extent.multiple_of(64)
            resolution = current_extent.shortest_side
        else:
            input = w.crop_image(input, bounds)
            resolution = bounds.extent.multiple_of(64).shortest_side
        result, _ = w.add(
            "MeshGraphormer-DepthMapPreprocessor",
            2,
            image=input,
            resolution=resolution,
            mask_type="based_on_depth",
            rand_seed=seed if seed != -1 else generate_seed(),
        )
        if bounds is not None:
            result = w.scale_image(result, bounds.extent)
            empty = w.empty_image(current_extent)
            result = w.composite_image_masked(result, empty, None, bounds.x, bounds.y)
    else:
        current_extent = current_extent.multiple_of(64)
        args = {"image": input, "resolution": current_extent.shortest_side}
        if mode is ControlMode.scribble:
            result = w.add("PiDiNetPreprocessor", 1, **args, safe="enable")
            result = w.add("ScribblePreprocessor", 1, image=result, resolution=args["resolution"])
        elif mode is ControlMode.line_art:
            result = w.add("LineArtPreprocessor", 1, **args, coarse="disable")
        elif mode is ControlMode.soft_edge:
            result = w.add("HEDPreprocessor", 1, **args, safe="disable")
        elif mode is ControlMode.depth:
            model = "depth_anything_vitb14.pth"
            result = w.add("DepthAnythingPreprocessor", 1, **args, ckpt_name=model)
        elif mode is ControlMode.normal:
            result = w.add("BAE-NormalMapPreprocessor", 1, **args)
        elif mode is ControlMode.pose:
            result = w.estimate_pose(**args)
        elif mode is ControlMode.segmentation:
            result = w.add("OneFormer-COCO-SemSegPreprocessor", 1, **args)

        assert result is not None

    if mode.is_lines:
        result = w.invert_image(result)
    if current_extent != extent.target:
        result = w.scale_image(result, extent.target)

    w.send_image(result)
    return w


def upscale_simple(w: ComfyWorkflow, image: Image, model: str, factor: float):
    upscale_model = w.load_upscale_model(model)
    img = w.load_image(image)
    img = w.upscale_image(upscale_model, img)
    if factor != 4.0:
        img = w.scale_image(img, image.extent * factor)
    w.send_image(img)
    return w


def upscale_tiled(
    w: ComfyWorkflow,
    image: Image,
    extent: ExtentInput,
    checkpoint: CheckpointInput,
    cond: Conditioning,
    sampling: SamplingInput,
    upscale_model_name: str,
    models: ModelDict,
):
    upscale_factor = extent.initial.width / extent.input.width
    layout = TileLayout.from_denoise_strength(
        extent.initial, extent.desired.width, sampling.denoise_strength
    )

    model, clip, vae = load_checkpoint_with_lora(w, checkpoint, models.all)
    model = apply_ip_adapter(w, model, cond.control, models)
    positive, negative = encode_text_prompt(w, cond, clip)

    in_image = w.load_image(image)
    if upscale_model_name:
        upscale_model = w.load_upscale_model(upscale_model_name)
        upscaled = w.upscale_image(upscale_model, in_image)
    else:
        upscaled = in_image
    if extent.input != extent.initial:
        upscaled = w.scale_image(upscaled, extent.initial)
    tile_layout = w.create_tile_layout(upscaled, layout.min_size, layout.padding, layout.blending)

    def tiled_control(control: Control, index: int):
        img = control.image.load(w, extent.initial, default_image=in_image)
        img = w.extract_image_tile(img, tile_layout, index)
        return Control(control.mode, ImageOutput(img), None, control.strength, control.range)

    def tiled_region(region: Region, index: int, tile_bounds: Bounds):
        region_bounds = Bounds.scale(region.bounds, upscale_factor)
        coverage = Bounds.intersection(tile_bounds, region_bounds).area / tile_bounds.area
        if coverage > 0.1:
            if region.mask:
                mask = region.mask.load(w, extent.initial)
                mask = w.extract_mask_tile(mask, tile_layout, index)
                region.mask = ImageOutput(mask, is_mask=True)
            return region
        return None

    out_image = upscaled
    for i in range(layout.total_tiles):
        bounds = layout.bounds(i)
        tile_image = w.extract_image_tile(upscaled, tile_layout, i)
        tile_mask = w.generate_tile_mask(tile_layout, i)

        tile_cond = cond.copy()
        regions = [tiled_region(r, i, bounds) for r in tile_cond.regions]
        tile_cond.regions = [r for r in regions if r is not None]
        tile_model = apply_attention_mask(w, model, tile_cond, clip, None)
        tile_model = apply_regional_ip_adapter(w, tile_model, tile_cond.regions, None, models)

        control = [tiled_control(c, i) for c in tile_cond.all_control]
        tile_pos, tile_neg = apply_control(w, positive, negative, control, None, models)

        latent = w.vae_encode(vae, tile_image)
        latent = w.set_latent_noise_mask(latent, tile_mask)
        sampler = w.ksampler_advanced(
            tile_model, tile_pos, tile_neg, latent, **_sampler_params(sampling)
        )
        tile_result = w.vae_decode(vae, sampler)
        out_image = w.merge_image_tile(out_image, tile_layout, i, tile_result)

    if extent.initial != extent.target:
        out_image = scale(extent.initial, extent.target, ScaleMode.resize, w, out_image, models)
    w.send_image(out_image)
    return w


###################################################################################################


def prepare(
    kind: WorkflowKind,
    canvas: Image | Extent,
    cond: ConditioningInput,
    style: Style,
    seed: int,
    models: ClientModels,
    perf: PerformanceSettings,
    mask: Mask | None = None,
    strength: float = 1.0,
    inpaint: InpaintParams | None = None,
    upscale_factor: float = 1.0,
    upscale_model: str = "",
    is_live: bool = False,
) -> WorkflowInput:
    """
    Takes UI model state, prepares images, normalizes inputs, and returns a WorkflowInput object
    which can be compared and serialized.
    """
    i = WorkflowInput(kind)
    i.conditioning = cond
    i.conditioning.positive, extra_loras = extract_loras(i.conditioning.positive, models.loras)
    i.conditioning.negative = merge_prompt(cond.negative, style.negative_prompt)
    i.conditioning.style = style.style_prompt
    for idx, region in enumerate(i.conditioning.regions):
        assert region.mask or idx == 0, "Only the first/bottom region can be without a mask"
        region.positive, region_loras = extract_loras(region.positive, models.loras)
        extra_loras += region_loras
    i.sampling = _sampling_from_style(style, strength, is_live)
    i.sampling.seed = seed
    i.models = style.get_models()
    i.models.loras = unique(i.models.loras + extra_loras, key=lambda l: l.name)
    _check_server_has_models(i.models, models, style.name)

    sd_version = i.models.version = models.version_of(style.sd_checkpoint)
    model_set = models.for_version(sd_version)
    has_ip_adapter = model_set.ip_adapter.find(ControlMode.reference) is not None
    i.models.loras += _get_sampling_lora(style, is_live, model_set, models)
    all_control = cond.control + [c for r in cond.regions for c in r.control]
    face_weight = median_or_zero(c.strength for c in all_control if c.mode is ControlMode.face)
    if face_weight > 0:
        i.models.loras.append(LoraInput(model_set.lora["face"], 0.65 * face_weight))

    if kind is WorkflowKind.generate:
        assert isinstance(canvas, Extent)
        i.images, i.batch_count = resolution.prepare_extent(
            canvas, sd_version, ensure(style), perf, downscale=not is_live
        )
        downscale_all_control_images(i.conditioning, canvas, i.images.extent.desired)

    elif kind is WorkflowKind.inpaint:
        assert isinstance(canvas, Image) and mask and inpaint and style
        i.images, _ = resolution.prepare_masked(canvas, mask, sd_version, style, perf)
        upscale_extent, _ = resolution.prepare_extent(
            mask.bounds.extent, sd_version, style, perf, downscale=False
        )
        i.inpaint = inpaint
        i.inpaint.use_reference = inpaint.use_reference and has_ip_adapter
        i.crop_upscale_extent = upscale_extent.extent.desired
        largest_extent = Extent.largest(i.images.extent.initial, upscale_extent.extent.desired)
        i.batch_count = resolution.compute_batch_size(largest_extent, 512, perf.batch_size)
        i.images.hires_mask = mask.to_image()
        scaling = ScaledExtent.from_input(i.images.extent).refinement_scaling
        if scaling in [ScaleMode.upscale_small, ScaleMode.upscale_quality]:
            i.images.hires_image = Image.crop(canvas, i.inpaint.target_bounds)
        if inpaint.mode is InpaintMode.remove_object and i.conditioning.positive == "":
            i.conditioning.positive = "background scenery"

    elif kind is WorkflowKind.refine:
        assert isinstance(canvas, Image) and style
        i.images, i.batch_count = resolution.prepare_image(
            canvas, sd_version, style, perf, downscale=False
        )
        downscale_all_control_images(i.conditioning, canvas.extent, i.images.extent.desired)

    elif kind is WorkflowKind.refine_region:
        assert isinstance(canvas, Image) and mask and inpaint and style
        allow_2pass = strength >= 0.7
        i.images, i.batch_count = resolution.prepare_masked(
            canvas, mask, sd_version, style, perf, downscale=allow_2pass
        )
        i.images.hires_mask = mask.to_image()
        i.inpaint = inpaint
        downscale_all_control_images(i.conditioning, canvas.extent, i.images.extent.desired)

    elif kind is WorkflowKind.upscale_tiled:
        assert isinstance(canvas, Image) and style
        if style.preferred_resolution > 0:
            tile_size = style.preferred_resolution
        else:
            tile_size = 1024 if sd_version is SDVersion.sdxl else 800
        tile_size = multiple_of(tile_size - 128, 8)
        tile_size = Extent(tile_size, tile_size)
        target_extent = canvas.extent * upscale_factor
        extent = ExtentInput(canvas.extent, target_extent.multiple_of(8), tile_size, target_extent)
        i.images = ImageInput(extent, canvas)
        i.upscale_model = upscale_model if upscale_factor > 1 else ""
        i.batch_count = 1

    else:
        raise Exception(f"Workflow {kind.name} not supported by this constructor")

    i.batch_count = 1 if is_live else i.batch_count
    return i


def prepare_upscale_simple(image: Image, model: str, factor: float):
    target_extent = image.extent * factor
    extent = ExtentInput(image.extent, image.extent, target_extent, target_extent)
    i = WorkflowInput(WorkflowKind.upscale_simple, ImageInput(extent, image))
    i.upscale_model = model
    return i


def prepare_create_control_image(
    image: Image,
    mode: ControlMode,
    performance_settings: PerformanceSettings,
    bounds: Bounds | None = None,
    seed: int = -1,
) -> WorkflowInput:
    i = WorkflowInput(WorkflowKind.control_image)
    i.control_mode = mode
    i.images = resolution.prepare_control(image, performance_settings)
    if bounds:
        seed = generate_seed() if seed == -1 else seed
        i.inpaint = InpaintParams(InpaintMode.fill, bounds)
        i.sampling = SamplingInput("", "", 1, 1, seed=seed)  # ignored apart from seed
    return i


def create(i: WorkflowInput, models: ClientModels, comfy_mode=ComfyRunMode.server) -> ComfyWorkflow:
    """
    Takes a WorkflowInput object and creates the corresponding ComfyUI workflow prompt.
    This should be a pure function, the workflow is entirely defined by the input.
    """
    workflow = ComfyWorkflow(models.node_inputs, comfy_mode)

    if i.kind is WorkflowKind.generate:
        return generate(
            workflow,
            ensure(i.models),
            ScaledExtent.from_input(i.extent),
            Conditioning.from_input(ensure(i.conditioning)),
            ensure(i.sampling),
            i.batch_count,
            models.for_checkpoint(ensure(i.models).checkpoint),
        )
    elif i.kind is WorkflowKind.inpaint:
        return inpaint(
            workflow,
            ensure(i.images),
            ensure(i.models),
            Conditioning.from_input(ensure(i.conditioning)),
            ensure(i.sampling),
            ensure(i.inpaint),
            ensure(i.crop_upscale_extent),
            i.batch_count,
            models.for_checkpoint(ensure(i.models).checkpoint),
        )
    elif i.kind is WorkflowKind.refine:
        return refine(
            workflow,
            i.image,
            ScaledExtent.from_input(i.extent),
            ensure(i.models),
            Conditioning.from_input(ensure(i.conditioning)),
            ensure(i.sampling),
            i.batch_count,
            models.for_checkpoint(ensure(i.models).checkpoint),
        )
    elif i.kind is WorkflowKind.refine_region:
        return refine_region(
            workflow,
            ensure(i.images),
            ensure(i.models),
            Conditioning.from_input(ensure(i.conditioning)),
            ensure(i.sampling),
            ensure(i.inpaint),
            i.batch_count,
            models.for_checkpoint(ensure(i.models).checkpoint),
        )
    elif i.kind is WorkflowKind.upscale_simple:
        return upscale_simple(workflow, i.image, i.upscale_model, i.upscale_factor)
    elif i.kind is WorkflowKind.upscale_tiled:
        return upscale_tiled(
            workflow,
            i.image,
            i.extent,
            ensure(i.models),
            Conditioning.from_input(ensure(i.conditioning)),
            ensure(i.sampling),
            i.upscale_model,
            models.for_checkpoint(ensure(i.models).checkpoint),
        )
    elif i.kind is WorkflowKind.control_image:
        return create_control_image(
            workflow,
            image=i.image,
            mode=i.control_mode,
            extent=ScaledExtent.from_input(i.extent),
            bounds=i.inpaint.target_bounds if i.inpaint else None,
            seed=i.sampling.seed if i.sampling else -1,
        )
    else:
        raise ValueError(f"Unsupported workflow kind: {i.kind}")


def _get_sampling_lora(style: Style, is_live: bool, model_set: ModelDict, models: ClientModels):
    sampler_name = style.live_sampler if is_live else style.sampler
    preset = SamplerPresets.instance()[sampler_name]
    if preset.lora:
        file = model_set.lora.find(preset.lora)
        if file is None and not preset.lora in models.loras:
            res = resources.search_path(ResourceKind.lora, model_set.version, preset.lora)
            if res is None and preset.lora == "lightning":
                raise ValueError(
                    f"The chosen sampler preset '{sampler_name}' requires LoRA "
                    f"'{preset.lora}', which is not supported by {model_set.version.value}."
                    " Please choose a different sampler."
                )
            elif res is None:
                raise ValueError(
                    f"Could not find LoRA '{preset.lora}' used by sampler preset '{sampler_name}'"
                )
            else:
                raise ValueError(
                    f"Could not find LoRA '{preset.lora}' ({', '.join(res)}) used by sampler preset '{sampler_name}'"
                )
        return [LoraInput(file or preset.lora, 1.0)]
    return []


def _check_server_has_models(input: CheckpointInput, models: ClientModels, style_name: str):
    if input.checkpoint not in models.checkpoints:
        raise ValueError(
            f"The checkpoint '{input.checkpoint}' used by style '{style_name}' is not available on the server"
        )
    for lora in input.loras:
        if lora.name not in models.loras:
            raise ValueError(
                f"The LoRA '{lora.name}' used by style '{style_name}' is not available on the server"
            )
    if input.vae != StyleSettings.vae.default and input.vae not in models.vae:
        raise ValueError(
            f"The VAE '{input.vae}' used by style '{style_name}' is not available on the server"
        )
