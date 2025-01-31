import hashlib
import os
import json
from server import PromptServer
from PIL import Image, ImageOps
import torch
import numpy as np
import folder_paths
from aiohttp import web
import io
import base64

import comfy.samplers
from .scribble_color_edit import ScribbleColorEditModel
from .llava_new import LLaVAModel

def tensor_to_base64(tensor):
    tensor = tensor.squeeze(0) * 255.
    pil_image = Image.fromarray(tensor.cpu().byte().numpy())
    buffered = io.BytesIO()
    pil_image.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    
    return img_str

def load_and_preprocess_image(image_path, convert_to='RGB', has_alpha=False):
    """Load and preprocess an image from a given path."""
    image = Image.open(image_path)
    image = ImageOps.exif_transpose(image)
    image = image.convert(convert_to)
    image_array = np.array(image).astype(np.float32) / 255.0
    image_tensor = torch.from_numpy(image_array)[None,]
    return image_tensor

def read_base64_image(base64_image):
    if base64_image.startswith("data:image/png;base64,"):
        base64_image = base64_image.split(",")[1]
    elif base64_image.startswith("data:image/jpeg;base64,"):
        base64_image = base64_image.split(",")[1]
    elif base64_image.startswith("data:image/webp;base64,"):
        base64_image = base64_image.split(",")[1]
    else:
        raise ValueError("Unsupported image format.")
    image_data = base64.b64decode(base64_image)
    image = Image.open(io.BytesIO(image_data))
    image = ImageOps.exif_transpose(image)
    return image

def load_and_resize_image(base64_image, convert_to='RGB', max_size=512):
    """Load and preprocess a base64 image, resize if necessary."""
    image = read_base64_image(base64_image)
    image = image.convert(convert_to)
    width, height = image.size
    if min(width, height) > max_size:
        scaling_factor = max_size / min(width, height)
        new_size = (int(width * scaling_factor), int(height * scaling_factor))
        image = image.resize(new_size, Image.LANCZOS)
    image_array = np.array(image).astype(np.float32) / 255.0
    image_tensor = torch.from_numpy(image_array)[None,]
    return image_tensor

def create_alpha_mask(image_path):
    """Create an alpha mask from the alpha channel of an image."""
    image = Image.open(image_path)
    image = ImageOps.exif_transpose(image)
    mask = torch.zeros((1, image.height, image.width), dtype=torch.float32, device="cpu")
    if 'A' in image.getbands():
        alpha_channel = np.array(image.getchannel('A')).astype(np.float32) / 255.0
        mask[0] = 1.0 - torch.from_numpy(alpha_channel)
    return mask

@PromptServer.instance.routes.post("/magic_quill/process_background_img")
async def process_background_img(request):
    img = await request.json()
    resized_img_tensor = load_and_resize_image(img)
    resized_img_base64 = "data:image/png;base64," + tensor_to_base64(resized_img_tensor)
    # add more processing here

    return web.json_response(resized_img_base64)

@PromptServer.instance.routes.post("/magic_quill/guess_prompt")
async def guess_prompt_handler(request):
    json_data = await request.json()
    add_color_image = json_data.get("add_color_image", None)
    original_image = json_data.get("original_image", None)
    add_edge_image = json_data.get("add_edge_image", None)

    original_image_path = folder_paths.get_annotated_filepath(original_image)
    original_image_tensor = load_and_preprocess_image(original_image_path)
    
    if add_color_image:
        add_color_image_path = folder_paths.get_annotated_filepath(add_color_image)
        add_color_image_tensor = load_and_preprocess_image(add_color_image_path)
    else:
        add_color_image_tensor = original_image_tensor
    
    width, height = original_image_tensor.shape[1], original_image_tensor.shape[2]
    add_edge_mask = create_alpha_mask(folder_paths.get_annotated_filepath(add_edge_image)) if add_edge_image else torch.zeros((1, height, width), dtype=torch.float32, device="cpu")

    res = MagicQuill.guess_prompt(original_image_tensor, add_color_image_tensor, add_edge_mask)

    return web.json_response({"prompt": res, "error": False})

class MagicQuill(object):
    scribbleColorEditModel = ScribbleColorEditModel()
    llavaModel = LLaVAModel()

    @classmethod
    def INPUT_TYPES(self):
        self.canvas_set = False

        work_dir = folder_paths.get_input_directory()
        imgs = [
            img
            for img in os.listdir(work_dir)
            if os.path.isfile(os.path.join(work_dir, img))
        ]
        imgs.append(None)

        return {
            "required": {
                "image": (imgs,),
                "original_image": (imgs,),
                "add_color_image": (imgs,),
                "add_edge_image": (imgs,),
                "remove_edge_image": (imgs,),

                "model": ("MODEL",),
                "clip": ("CLIP",),
                "vae": ("VAE",),
                
                "base_model_version": (['SD1.5'], {"default": "SD1.5"}),
                "positive_prompt": ("STRING", {"default": ""}),
                "negative_prompt": ("STRING", {"default": ""}),
                "dtype": (['float16', 'bfloat16', 'float32', 'float64'], {"default": "float16"}),
                "stroke_as_edge": (['enable', 'disable'], {"default": "enable"}),
                "fine_edge": (['enable', 'disable'], {"default": "disable"}),

                "grow_size": ("INT", {"default": 15, "min": 0, "max": 100, "step": 1, "display": "slider"}),
                "edge_strength": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 5.0, "step": 0.01, "display": "slider"}),
                "color_strength": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 5.0, "step": 0.01, "display": "slider"}),
                # "palette_resolution": ("INT", {"default": 2048, "min": 128, "max": 2048, "step": 16, "display": "slider"}),
                "inpaint_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 5.0, "step": 0.01, "display": "slider"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 50, "display": "slider"}),
                "cfg": ("FLOAT", {"default": 4.0, "min": 0.0, "max": 100.0, "step":0.1, "round": 0.01, "display": "slider"}),
                "sampler_name": (comfy.samplers.KSampler.SAMPLERS, {"default": "euler_ancestral"}),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS, {"default": "exponential"}),
            },
        }

    RETURN_TYPES = ("LATENT", "IMAGE", "IMAGE", "IMAGE")
    RETURN_NAMES = ("latent", "image", "edge map", "color palette")

    FUNCTION = "painter_execute"
    CATEGORY = "image"

    @classmethod
    def prepare_images_and_masks(cls, image, original_image, add_color_image, add_edge_image, remove_edge_image):
        image_path = folder_paths.get_annotated_filepath(image)
        image_tensor = load_and_preprocess_image(image_path)
        
        width, height = image_tensor.shape[1], image_tensor.shape[2]
        
        total_mask = create_alpha_mask(image_path)
        
        original_image_path = folder_paths.get_annotated_filepath(original_image)
        original_image_tensor = load_and_preprocess_image(original_image_path)
        
        if add_color_image:
            add_color_image_path = folder_paths.get_annotated_filepath(add_color_image)
            add_color_image_tensor = load_and_preprocess_image(add_color_image_path)
        else:
            add_color_image_tensor = original_image_tensor
        
        add_edge_mask = create_alpha_mask(folder_paths.get_annotated_filepath(add_edge_image)) if add_edge_image else torch.zeros_like(total_mask)
        
        remove_edge_mask = create_alpha_mask(folder_paths.get_annotated_filepath(remove_edge_image)) if remove_edge_image else torch.zeros_like(total_mask)
        
        return add_color_image_tensor, original_image_tensor, total_mask, add_edge_mask, remove_edge_mask

    @classmethod
    def guess_prompt(cls, original_image_tensor, add_color_image_tensor, add_edge_mask):
        description, ans1, ans2 = cls.llavaModel.process(original_image_tensor, add_color_image_tensor, add_edge_mask)
        ans_list = []
        if ans1 and ans1 != "":
            ans_list.append(ans1)
        if ans2 and ans2 != "":
            ans_list.append(ans2)

        return ", ".join(ans_list)

    @classmethod
    def painter_execute(cls, image, original_image, add_color_image, add_edge_image, remove_edge_image, model, vae, clip, base_model_version, positive_prompt, negative_prompt, dtype, grow_size, stroke_as_edge, fine_edge, edge_strength, color_strength, inpaint_strength, seed, steps, cfg, sampler_name, scheduler):
        print(image, original_image, add_color_image, add_edge_image, remove_edge_image, model, vae, clip, base_model_version, positive_prompt, negative_prompt, dtype, grow_size, edge_strength, color_strength, inpaint_strength, seed, steps, cfg, sampler_name, scheduler)
        add_color_image, original_image, total_mask, add_edge_mask, remove_edge_mask = cls.prepare_images_and_masks(image, original_image, add_color_image, add_edge_image, remove_edge_image)

        if torch.sum(remove_edge_mask).item() > 0 and torch.sum(add_edge_mask).item() == 0:
            if positive_prompt == "":
                positive_prompt = "empty scene"
            edge_strength /= 3.

        if not positive_prompt or positive_prompt == "":
            positive_prompt = cls.guess_prompt(original_image, add_color_image, add_edge_mask)

        print("positive prompt: ", positive_prompt)
        latent_samples, final_image, lineart_output, color_output = cls.scribbleColorEditModel.process(model, vae, clip, original_image, add_color_image, base_model_version, positive_prompt, negative_prompt, dtype, total_mask, add_edge_mask, remove_edge_mask, grow_size, stroke_as_edge, fine_edge, edge_strength, color_strength, inpaint_strength, seed, steps, cfg, sampler_name, scheduler)

        final_image_base64 = tensor_to_base64(final_image)
        PromptServer.instance.send_sync(
            "magic_quill/final_image", {"image": final_image_base64, "image_name": image}
        )
        
        return (latent_samples, final_image, lineart_output, color_output)

    @classmethod
    def IS_CHANGED(self, image, original_image, add_color_image, add_edge_image, remove_edge_image, model, vae, clip, base_model_version, positive_prompt, negative_prompt, dtype, grow_size, edge_strength, color_strength, inpaint_strength, seed, steps, cfg, sampler_name, scheduler):
        image_path = folder_paths.get_annotated_filepath(image)
        m = hashlib.sha256()
        with open(image_path, "rb") as f:
            m.update(f.read())

        return m.digest().hex()

    @classmethod
    def VALIDATE_INPUTS(self, image, original_image, add_color_image, add_edge_image, remove_edge_image, model, vae, clip, base_model_version, positive_prompt, negative_prompt, dtype, grow_size, edge_strength, color_strength, inpaint_strength, seed, steps, cfg, sampler_name, scheduler):
        if not folder_paths.exists_annotated_filepath(image):
            return "Invalid image file: {}".format(image)

        return True