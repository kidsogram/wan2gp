import os
import re
import time
from dataclasses import dataclass
from glob import iglob
from mmgp import offload as offload
import torch
from wan.utils.utils import calculate_new_dimensions
from flux.sampling import denoise, get_schedule, prepare_kontext, unpack
from flux.modules.layers import get_linear_split_map
from flux.util import (
    aspect_ratio_to_height_width,
    load_ae,
    load_clip,
    load_flow_model,
    load_t5,
    save_image,
)

class model_factory:
    def __init__(
        self,
        checkpoint_dir,
        model_filename = None,
        model_type = None, 
        model_def = None,
        base_model_type = None,
        text_encoder_filename = None,
        quantizeTransformer = False,
        save_quantized = False,
        dtype = torch.bfloat16,
        VAE_dtype = torch.float32,
        mixed_precision_transformer = False
    ):
        self.device = torch.device(f"cuda")
        self.VAE_dtype = VAE_dtype
        self.dtype = dtype
        torch_device = "cpu"
        # model_filename = ["c:/temp/flux1-schnell.safetensors"] 
        
        self.t5 = load_t5(torch_device, text_encoder_filename, max_length=512)
        self.clip = load_clip(torch_device)
        self.name = model_def.get("flux-model", "flux-dev")
        # self.name= "flux-dev-kontext"
        # self.name= "flux-dev"
        # self.name= "flux-schnell"
        self.model = load_flow_model(self.name, model_filename[0], torch_device)

        self.vae = load_ae(self.name, device=torch_device)

        # offload.change_dtype(self.model, dtype, True)
        # offload.save_model(self.model, "flux-dev.safetensors")
        if save_quantized:
            from wgp import save_quantized_model
            save_quantized_model(self.model, model_type, model_filename[0], dtype, None)

        split_linear_modules_map = get_linear_split_map()
        self.model.split_linear_modules_map = split_linear_modules_map
        offload.split_linear_modules(self.model, split_linear_modules_map )

    
    def generate(
            self,
            seed: int | None = None,
            input_prompt: str = "replace the logo with the text 'Black Forest Labs'",
            sampling_steps: int = 20,
            input_ref_images = None,
            width= 832,
            height=480,
            embedded_guidance_scale: float = 2.5,
            fit_into_canvas = None,
            callback = None,
            loras_slists = None,
            batch_size = 1,
            **bbargs
    ):
            
            if self._interrupt:
                return None

            device="cuda"
            if input_ref_images != None and len(input_ref_images) > 0: 
                image_ref = input_ref_images[0]
                w, h = image_ref.size
                height, width = calculate_new_dimensions(height, width, h, w, fit_into_canvas)
            else:
                image_ref = None

            inp, height, width = prepare_kontext(
                t5=self.t5,
                clip=self.clip,
                prompt=input_prompt,
                ae=self.vae,
                img_cond=image_ref,
                target_width=width,
                target_height=height,
                bs=batch_size,
                seed=seed,
                device=device,
            )

            inp.pop("img_cond_orig")
            timesteps = get_schedule(sampling_steps, inp["img"].shape[1], shift=(self.name != "flux-schnell"))
            def unpack_latent(x):
                return unpack(x.float(), height, width) 
            # denoise initial noise
            x = denoise(self.model, **inp, timesteps=timesteps, guidance=embedded_guidance_scale, callback=callback, pipeline=self, loras_slists= loras_slists, unpack_latent = unpack_latent)
            if x==None: return None
            # decode latents to pixel space
            x = unpack_latent(x)
            with torch.autocast(device_type=device, dtype=torch.bfloat16):
                x = self.vae.decode(x)

            x = x.clamp(-1, 1)
            x = x.transpose(0, 1)
            return x

def query_model_def(model_type, model_def):
    flux_model = model_def.get("flux-model", "flux-dev")
    flux_schnell = flux_model == "flux-schnell" 
    model_def_output = {
        "image_outputs" : True,
    }
    if flux_schnell:
        model_def_output["no_guidance"] = True

    return model_def_output