from diffusers_helper.hf_login import login

import os

os.environ['HF_HOME'] = os.path.abspath(os.path.realpath(os.path.join(os.path.dirname(__file__), './hf_download')))

import gradio as gr
import torch
import traceback
import einops
import safetensors.torch as sf
import numpy as np
import argparse
import math

from PIL import Image
from diffusers import AutoencoderKLHunyuanVideo
from transformers import LlamaModel, CLIPTextModel, LlamaTokenizerFast, CLIPTokenizer
from diffusers_helper.hunyuan import encode_prompt_conds, vae_decode, vae_encode, vae_decode_fake
from diffusers_helper.utils import save_bcthw_as_mp4, crop_or_pad_yield_mask, soft_append_bcthw, resize_and_center_crop, state_dict_weighted_merge, state_dict_offset_merge, generate_timestamp
from diffusers_helper.models.hunyuan_video_packed import HunyuanVideoTransformer3DModelPacked
from diffusers_helper.pipelines.k_diffusion_hunyuan import sample_hunyuan
from diffusers_helper.memory import cpu, gpu, get_cuda_free_memory_gb, move_model_to_device_with_memory_preservation, offload_model_from_device_for_memory_preservation, fake_diffusers_current_device, DynamicSwapInstaller, unload_complete_models, load_model_as_complete
from diffusers_helper.thread_utils import AsyncStream, async_run
from diffusers_helper.gradio.progress_bar import make_progress_bar_css, make_progress_bar_html
from transformers import SiglipImageProcessor, SiglipVisionModel
from diffusers_helper.clip_vision import hf_clip_vision_encode
from diffusers_helper.bucket_tools import find_nearest_bucket


parser = argparse.ArgumentParser()
parser.add_argument('--share', action='store_true')
parser.add_argument("--server", type=str, default='0.0.0.0')
parser.add_argument("--port", type=int, required=False)
parser.add_argument("--inbrowser", action='store_true')
args = parser.parse_args()

# for win desktop probably use --server 127.0.0.1 --inbrowser
# For linux server probably use --server 127.0.0.1 or do not use any cmd flags

print(args)

free_mem_gb = get_cuda_free_memory_gb(gpu)
high_vram = free_mem_gb > 60

print(f'Free VRAM {free_mem_gb} GB')
print(f'High-VRAM Mode: {high_vram}')

text_encoder = LlamaModel.from_pretrained("hunyuanvideo-community/HunyuanVideo", subfolder='text_encoder', torch_dtype=torch.float16).cpu()
text_encoder_2 = CLIPTextModel.from_pretrained("hunyuanvideo-community/HunyuanVideo", subfolder='text_encoder_2', torch_dtype=torch.float16).cpu()
tokenizer = LlamaTokenizerFast.from_pretrained("hunyuanvideo-community/HunyuanVideo", subfolder='tokenizer')
tokenizer_2 = CLIPTokenizer.from_pretrained("hunyuanvideo-community/HunyuanVideo", subfolder='tokenizer_2')
vae = AutoencoderKLHunyuanVideo.from_pretrained("hunyuanvideo-community/HunyuanVideo", subfolder='vae', torch_dtype=torch.float16).cpu()

feature_extractor = SiglipImageProcessor.from_pretrained("lllyasviel/flux_redux_bfl", subfolder='feature_extractor')
image_encoder = SiglipVisionModel.from_pretrained("lllyasviel/flux_redux_bfl", subfolder='image_encoder', torch_dtype=torch.float16).cpu()

transformer = HunyuanVideoTransformer3DModelPacked.from_pretrained('lllyasviel/FramePackI2V_HY', torch_dtype=torch.bfloat16).cpu()

vae.eval()
text_encoder.eval()
text_encoder_2.eval()
image_encoder.eval()
transformer.eval()

if not high_vram:
    vae.enable_slicing()
    vae.enable_tiling()

transformer.high_quality_fp32_output_for_inference = True
print('transformer.high_quality_fp32_output_for_inference = True')

transformer.to(dtype=torch.bfloat16)
vae.to(dtype=torch.float16)
image_encoder.to(dtype=torch.float16)
text_encoder.to(dtype=torch.float16)
text_encoder_2.to(dtype=torch.float16)

vae.requires_grad_(False)
text_encoder.requires_grad_(False)
text_encoder_2.requires_grad_(False)
image_encoder.requires_grad_(False)
transformer.requires_grad_(False)

if not high_vram:
    # DynamicSwapInstaller is same as huggingface's enable_sequential_offload but 3x faster
    DynamicSwapInstaller.install_model(transformer, device=gpu)
    DynamicSwapInstaller.install_model(text_encoder, device=gpu)
else:
    text_encoder.to(gpu)
    text_encoder_2.to(gpu)
    image_encoder.to(gpu)
    vae.to(gpu)
    transformer.to(gpu)

stream = AsyncStream()

outputs_folder = './outputs/'
os.makedirs(outputs_folder, exist_ok=True)


@torch.no_grad()
def worker(input_image, prompt_1, prompt_2, prompt_3, prompt_4, prompt_5, n_prompt, seed, total_second_length, latent_window_size, steps, cfg, gs, rs, gpu_memory_preservation, use_teacache, mp4_crf):
    total_target_latent_frames = math.ceil((total_second_length * 30) / 4)
    total_latent_sections = math.ceil(total_target_latent_frames / latent_window_size)
    total_latent_sections = int(max(total_latent_sections, 1))

    # Calculate transition points (latent frame indices from the start) for 5 segments
    transition_point_1 = total_target_latent_frames / 5
    transition_point_2 = 2 * total_target_latent_frames / 5
    transition_point_3 = 3 * total_target_latent_frames / 5
    transition_point_4 = 4 * total_target_latent_frames / 5

    job_id = generate_timestamp()

    stream.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'Starting ...'))))

    try:
        # Clean GPU
        if not high_vram:
            unload_complete_models(
                text_encoder, text_encoder_2, image_encoder, vae, transformer
            )

        # Text encoding

        stream.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'Text encoding ...'))))

        if not high_vram:
            fake_diffusers_current_device(text_encoder, gpu)  # since we only encode one text - that is one model move and one encode, offload is same time consumption since it is also one load and one encode.
            load_model_as_complete(text_encoder_2, target_device=gpu)

        # Encode all five prompts
        llama_vec_1, clip_l_pooler_1 = encode_prompt_conds(prompt_1, text_encoder, text_encoder_2, tokenizer, tokenizer_2)
        llama_vec_2, clip_l_pooler_2 = encode_prompt_conds(prompt_2, text_encoder, text_encoder_2, tokenizer, tokenizer_2)
        llama_vec_3, clip_l_pooler_3 = encode_prompt_conds(prompt_3, text_encoder, text_encoder_2, tokenizer, tokenizer_2)
        llama_vec_4, clip_l_pooler_4 = encode_prompt_conds(prompt_4, text_encoder, text_encoder_2, tokenizer, tokenizer_2)
        llama_vec_5, clip_l_pooler_5 = encode_prompt_conds(prompt_5, text_encoder, text_encoder_2, tokenizer, tokenizer_2)

        # Encode negative prompt (only one needed)
        if n_prompt and cfg != 1: # Only encode if n_prompt is given and cfg > 1
            llama_vec_n, clip_l_pooler_n = encode_prompt_conds(n_prompt, text_encoder, text_encoder_2, tokenizer, tokenizer_2)
        else:
            # Use zero embeddings if no n_prompt or cfg is 1
            temp_llama_vec, _ = encode_prompt_conds("", text_encoder, text_encoder_2, tokenizer, tokenizer_2)
            llama_vec_n = torch.zeros_like(temp_llama_vec)
            clip_l_pooler_n = torch.zeros_like(clip_l_pooler_1) # Use any pooler for shape

        # Pad/crop embeddings
        llama_vec_1, llama_attention_mask_1 = crop_or_pad_yield_mask(llama_vec_1, length=512)
        llama_vec_2, llama_attention_mask_2 = crop_or_pad_yield_mask(llama_vec_2, length=512)
        llama_vec_3, llama_attention_mask_3 = crop_or_pad_yield_mask(llama_vec_3, length=512)
        llama_vec_4, llama_attention_mask_4 = crop_or_pad_yield_mask(llama_vec_4, length=512)
        llama_vec_5, llama_attention_mask_5 = crop_or_pad_yield_mask(llama_vec_5, length=512)
        llama_vec_n, llama_attention_mask_n = crop_or_pad_yield_mask(llama_vec_n, length=512)

        # Processing input image

        stream.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'Image processing ...'))))

        H, W, C = input_image.shape
        height, width = find_nearest_bucket(H, W, resolution=640)
        input_image_np = resize_and_center_crop(input_image, target_width=width, target_height=height)

        Image.fromarray(input_image_np).save(os.path.join(outputs_folder, f'{job_id}.png'))

        input_image_pt = torch.from_numpy(input_image_np).float() / 127.5 - 1
        input_image_pt = input_image_pt.permute(2, 0, 1)[None, :, None]

        # VAE encoding

        stream.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'VAE encoding ...'))))

        if not high_vram:
            load_model_as_complete(vae, target_device=gpu)

        start_latent = vae_encode(input_image_pt, vae)

        # CLIP Vision

        stream.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'CLIP Vision encoding ...'))))

        if not high_vram:
            load_model_as_complete(image_encoder, target_device=gpu)

        image_encoder_output = hf_clip_vision_encode(input_image_np, feature_extractor, image_encoder)
        image_encoder_last_hidden_state = image_encoder_output.last_hidden_state

        # Dtype conversion for all embeddings
        llama_vec_1 = llama_vec_1.to(transformer.dtype)
        llama_vec_2 = llama_vec_2.to(transformer.dtype)
        llama_vec_3 = llama_vec_3.to(transformer.dtype)
        llama_vec_4 = llama_vec_4.to(transformer.dtype)
        llama_vec_5 = llama_vec_5.to(transformer.dtype)
        llama_vec_n = llama_vec_n.to(transformer.dtype)
        clip_l_pooler_1 = clip_l_pooler_1.to(transformer.dtype)
        clip_l_pooler_2 = clip_l_pooler_2.to(transformer.dtype)
        clip_l_pooler_3 = clip_l_pooler_3.to(transformer.dtype)
        clip_l_pooler_4 = clip_l_pooler_4.to(transformer.dtype)
        clip_l_pooler_5 = clip_l_pooler_5.to(transformer.dtype)
        clip_l_pooler_n = clip_l_pooler_n.to(transformer.dtype)
        image_encoder_last_hidden_state = image_encoder_last_hidden_state.to(transformer.dtype)

        # Sampling

        stream.output_queue.push(('progress', (None, '', make_progress_bar_html(0, 'Start sampling ...'))))

        rnd = torch.Generator("cpu").manual_seed(seed)
        num_frames = latent_window_size * 4 - 3

        history_latents = torch.zeros(size=(1, 16, 1 + 2 + 16, height // 8, width // 8), dtype=torch.float32).cpu()
        history_pixels = None
        total_generated_latent_frames = 0

        latent_paddings = reversed(range(total_latent_sections))

        if total_latent_sections > 4:
            # In theory the latent_paddings should follow the above sequence, but it seems that duplicating some
            # items looks better than expanding it when total_latent_sections > 4
            # One can try to remove below trick and just
            # use `latent_paddings = list(reversed(range(total_latent_sections)))` to compare
            latent_paddings = [3] + [2] * (total_latent_sections - 3) + [1, 0]

        for i, latent_padding in enumerate(latent_paddings):
            is_last_section = latent_padding == 0
            latent_padding_size = latent_padding * latent_window_size

            if stream.input_queue.top() == 'end':
                stream.output_queue.push(('end', None))
                return

            print(f'latent_padding_size = {latent_padding_size}, is_last_section = {is_last_section}')

            indices = torch.arange(0, sum([1, latent_padding_size, latent_window_size, 1, 2, 16])).unsqueeze(0)
            clean_latent_indices_pre, blank_indices, latent_indices, clean_latent_indices_post, clean_latent_2x_indices, clean_latent_4x_indices = indices.split([1, latent_padding_size, latent_window_size, 1, 2, 16], dim=1)
            clean_latent_indices = torch.cat([clean_latent_indices_pre, clean_latent_indices_post], dim=1)

            clean_latents_pre = start_latent.to(history_latents)
            clean_latents_post, clean_latents_2x, clean_latents_4x = history_latents[:, :, :1 + 2 + 16, :, :].split([1, 2, 16], dim=2)
            clean_latents = torch.cat([clean_latents_pre, clean_latents_post], dim=2)

            # Determine which prompt embeddings to use for this section
            # Calculate the midpoint frame index (from the start) for this section
            # Frame indices count from the *start* of the total video duration
            num_latent_frames_in_section = latent_window_size
            # Calculate the range of latent frames (indices from start) this section corresponds to.
            # The loop iterates backwards from the end (high latent_padding) to the start (latent_padding=0).
            # We need the original index `section_idx` corresponding to the *forward* pass (0 to N-1)
            section_idx = (total_latent_sections - 1) - i # Approximate forward index
            frame_idx_start = section_idx * latent_window_size
            frame_idx_end = frame_idx_start + num_latent_frames_in_section
            mid_frame_idx = (frame_idx_start + frame_idx_end) / 2.0

            if mid_frame_idx < transition_point_1:
                current_llama_vec = llama_vec_1
                current_mask = llama_attention_mask_1
                current_pooler = clip_l_pooler_1
                print(f"Section {i} (padding {latent_padding}): Using Prompt 1 (Mid frame {mid_frame_idx:.1f} < {transition_point_1:.1f})")
            elif mid_frame_idx < transition_point_2:
                current_llama_vec = llama_vec_2
                current_mask = llama_attention_mask_2
                current_pooler = clip_l_pooler_2
                print(f"Section {i} (padding {latent_padding}): Using Prompt 2 ({transition_point_1:.1f} <= Mid frame {mid_frame_idx:.1f} < {transition_point_2:.1f})")
            elif mid_frame_idx < transition_point_3:
                current_llama_vec = llama_vec_3
                current_mask = llama_attention_mask_3
                current_pooler = clip_l_pooler_3
                print(f"Section {i} (padding {latent_padding}): Using Prompt 3 ({transition_point_2:.1f} <= Mid frame {mid_frame_idx:.1f} < {transition_point_3:.1f})")
            elif mid_frame_idx < transition_point_4:
                current_llama_vec = llama_vec_4
                current_mask = llama_attention_mask_4
                current_pooler = clip_l_pooler_4
                print(f"Section {i} (padding {latent_padding}): Using Prompt 4 ({transition_point_3:.1f} <= Mid frame {mid_frame_idx:.1f} < {transition_point_4:.1f})")
            else:
                current_llama_vec = llama_vec_5
                current_mask = llama_attention_mask_5
                current_pooler = clip_l_pooler_5
                print(f"Section {i} (padding {latent_padding}): Using Prompt 5 (Mid frame {mid_frame_idx:.1f} >= {transition_point_4:.1f})")

            if not high_vram:
                unload_complete_models()
                move_model_to_device_with_memory_preservation(transformer, target_device=gpu, preserved_memory_gb=gpu_memory_preservation)

            if use_teacache:
                transformer.initialize_teacache(enable_teacache=True, num_steps=steps)
            else:
                transformer.initialize_teacache(enable_teacache=False)

            def callback(d):
                preview = d['denoised']
                preview = vae_decode_fake(preview)

                preview = (preview * 255.0).detach().cpu().numpy().clip(0, 255).astype(np.uint8)
                preview = einops.rearrange(preview, 'b c t h w -> (b h) (t w) c')

                if stream.input_queue.top() == 'end':
                    stream.output_queue.push(('end', None))
                    raise KeyboardInterrupt('User ends the task.')

                current_step = d['i'] + 1
                percentage = int(100.0 * current_step / steps)
                hint = f'Sampling {current_step}/{steps}'
                desc = f'Total generated frames: {int(max(0, total_generated_latent_frames * 4 - 3))}, Video length: {max(0, (total_generated_latent_frames * 4 - 3) / 30) :.2f} seconds (FPS-30). The video is being extended now ...'
                stream.output_queue.push(('progress', (preview, desc, make_progress_bar_html(percentage, hint))))
                return

            generated_latents = sample_hunyuan(
                transformer=transformer,
                sampler='unipc',
                width=width,
                height=height,
                frames=num_frames,
                real_guidance_scale=cfg,
                distilled_guidance_scale=gs,
                guidance_rescale=rs,
                # shift=3.0,
                num_inference_steps=steps,
                generator=rnd,
                prompt_embeds=current_llama_vec,           # Use selected prompt
                prompt_embeds_mask=current_mask,           # Use selected mask
                prompt_poolers=current_pooler,             # Use selected pooler
                negative_prompt_embeds=llama_vec_n,
                negative_prompt_embeds_mask=llama_attention_mask_n,
                negative_prompt_poolers=clip_l_pooler_n,
                device=gpu,
                dtype=torch.bfloat16,
                image_embeddings=image_encoder_last_hidden_state,
                latent_indices=latent_indices,
                clean_latents=clean_latents,
                clean_latent_indices=clean_latent_indices,
                clean_latents_2x=clean_latents_2x,
                clean_latent_2x_indices=clean_latent_2x_indices,
                clean_latents_4x=clean_latents_4x,
                clean_latent_4x_indices=clean_latent_4x_indices,
                callback=callback,
            )

            if is_last_section:
                generated_latents = torch.cat([start_latent.to(generated_latents), generated_latents], dim=2)

            total_generated_latent_frames += int(generated_latents.shape[2])
            history_latents = torch.cat([generated_latents.to(history_latents), history_latents], dim=2)

            if not high_vram:
                offload_model_from_device_for_memory_preservation(transformer, target_device=gpu, preserved_memory_gb=8)
                load_model_as_complete(vae, target_device=gpu)

            real_history_latents = history_latents[:, :, :total_generated_latent_frames, :, :]

            if history_pixels is None:
                history_pixels = vae_decode(real_history_latents, vae).cpu()
            else:
                section_latent_frames = (latent_window_size * 2 + 1) if is_last_section else (latent_window_size * 2)
                overlapped_frames = latent_window_size * 4 - 3

                current_pixels = vae_decode(real_history_latents[:, :, :section_latent_frames], vae).cpu()
                history_pixels = soft_append_bcthw(current_pixels, history_pixels, overlapped_frames)

            if not high_vram:
                unload_complete_models()

            output_filename = os.path.join(outputs_folder, f'{job_id}_{total_generated_latent_frames}.mp4')

            save_bcthw_as_mp4(history_pixels, output_filename, fps=30, crf=mp4_crf)

            print(f'Decoded. Current latent shape {real_history_latents.shape}; pixel shape {history_pixels.shape}')

            stream.output_queue.push(('file', output_filename))

            if is_last_section:
                break
    except:
        traceback.print_exc()

        if not high_vram:
            unload_complete_models(
                text_encoder, text_encoder_2, image_encoder, vae, transformer
            )

    stream.output_queue.push(('end', None))
    return


def process(input_image, prompt_1, prompt_2, prompt_3, prompt_4, prompt_5, n_prompt, seed, total_second_length, latent_window_size, steps, cfg, gs, rs, gpu_memory_preservation, use_teacache, mp4_crf):
    global stream
    assert input_image is not None, 'No input image!'

    # Filter out empty prompts and ensure at least one exists
    prompts = [p for p in [prompt_1, prompt_2, prompt_3, prompt_4, prompt_5] if p and p.strip()]
    assert prompts, 'At least one prompt must be provided!'

    # If fewer than 5 prompts are provided, distribute them (simple fill forward/backward)
    # This is a basic strategy; more complex interpolation could be used.
    if len(prompts) < 5:
        filled_prompts = ["" for _ in range(5)]
        if len(prompts) == 1:
            filled_prompts = [prompts[0]] * 5
        elif len(prompts) == 2:
            filled_prompts[0] = prompts[0]
            filled_prompts[1] = prompts[0]
            filled_prompts[2] = prompts[1] # Middle leans towards end
            filled_prompts[3] = prompts[1]
            filled_prompts[4] = prompts[1]
        elif len(prompts) == 3:
            filled_prompts[0] = prompts[0]
            filled_prompts[1] = prompts[0]
            filled_prompts[2] = prompts[1]
            filled_prompts[3] = prompts[2]
            filled_prompts[4] = prompts[2]
        elif len(prompts) == 4:
            filled_prompts[0] = prompts[0]
            filled_prompts[1] = prompts[1]
            filled_prompts[2] = prompts[2]
            filled_prompts[3] = prompts[3]
            filled_prompts[4] = prompts[3] # Last one duplicates
        prompt_1, prompt_2, prompt_3, prompt_4, prompt_5 = filled_prompts
    else:
        prompt_1, prompt_2, prompt_3, prompt_4, prompt_5 = prompts[:5]

    yield None, None, '', '', gr.update(interactive=False), gr.update(interactive=True)

    stream = AsyncStream()

    async_run(worker, input_image, prompt_1, prompt_2, prompt_3, prompt_4, prompt_5, n_prompt, seed, total_second_length, latent_window_size, steps, cfg, gs, rs, gpu_memory_preservation, use_teacache, mp4_crf)

    output_filename = None

    while True:
        flag, data = stream.output_queue.next()

        if flag == 'file':
            output_filename = data
            yield output_filename, gr.update(), gr.update(), gr.update(), gr.update(interactive=False), gr.update(interactive=True)

        if flag == 'progress':
            preview, desc, html = data
            yield gr.update(), gr.update(visible=True, value=preview), desc, html, gr.update(interactive=False), gr.update(interactive=True)

        if flag == 'end':
            yield output_filename, gr.update(visible=False), gr.update(), '', gr.update(interactive=True), gr.update(interactive=False)
            break


def end_process():
    stream.input_queue.push('end')


quick_prompts = [
    'The girl dances gracefully, with clear movements, full of charm.',
    'A character doing some simple body movements.',
]
quick_prompts = [[x] for x in quick_prompts]
# Modify quick prompts for five inputs
quick_prompts = [
    ['A car driving down a road during the day.', 'The car continues driving as the sun sets.', 'The car drives through the city at night.', 'The car drives over a bridge.', 'The car arrives at its destination.'],
    ['A seed sprouts from the ground.', 'A small plant grows taller.', 'The plant develops leaves.', 'The plant buds.', 'The plant blossoms with flowers.'],
]


css = make_progress_bar_css()
block = gr.Blocks(css=css).queue()
with block:
    gr.Markdown('# FramePack - Prompt Interpolation')
    with gr.Row():
        with gr.Column():
            input_image = gr.Image(sources='upload', type="numpy", label="Image", height=320)
            prompt_1 = gr.Textbox(label="Prompt 1 (Start)", value='')
            prompt_2 = gr.Textbox(label="Prompt 2", value='')
            prompt_3 = gr.Textbox(label="Prompt 3", value='')
            prompt_4 = gr.Textbox(label="Prompt 4", value='')
            prompt_5 = gr.Textbox(label="Prompt 5 (End)", value='')
            example_quick_prompts = gr.Dataset(samples=quick_prompts, label='Quick Prompt Sets', samples_per_page=1000, components=[prompt_1, prompt_2, prompt_3, prompt_4, prompt_5])
            example_quick_prompts.click(lambda x: x, inputs=[example_quick_prompts], outputs=[prompt_1, prompt_2, prompt_3, prompt_4, prompt_5], show_progress=False, queue=False)

            with gr.Row():
                start_button = gr.Button(value="Start Generation")
                end_button = gr.Button(value="End Generation", interactive=False)

            with gr.Group():
                use_teacache = gr.Checkbox(label='Use TeaCache', value=True, info='Faster speed, but often makes hands and fingers slightly worse.')

                n_prompt = gr.Textbox(label="Negative Prompt", value="", visible=False)  # Not used
                seed = gr.Number(label="Seed", value=31337, precision=0)

                total_second_length = gr.Slider(label="Total Video Length (Seconds)", minimum=1, maximum=120, value=5, step=0.1)
                latent_window_size = gr.Slider(label="Latent Window Size", minimum=1, maximum=33, value=9, step=1, visible=False)  # Should not change
                steps = gr.Slider(label="Steps", minimum=1, maximum=100, value=25, step=1, info='Changing this value is not recommended.')

                cfg = gr.Slider(label="CFG Scale", minimum=1.0, maximum=32.0, value=1.0, step=0.01, visible=False)  # Should not change
                gs = gr.Slider(label="Distilled CFG Scale", minimum=1.0, maximum=32.0, value=10.0, step=0.01, info='Changing this value is not recommended.')
                rs = gr.Slider(label="CFG Re-Scale", minimum=0.0, maximum=1.0, value=0.0, step=0.01, visible=False)  # Should not change

                gpu_memory_preservation = gr.Slider(label="GPU Inference Preserved Memory (GB) (larger means slower)", minimum=6, maximum=128, value=6, step=0.1, info="Set this number to a larger value if you encounter OOM. Larger value causes slower speed.")

                mp4_crf = gr.Slider(label="MP4 Compression", minimum=0, maximum=100, value=16, step=1, info="Lower means better quality. 0 is uncompressed. Change to 16 if you get black outputs. ")

        with gr.Column():
            preview_image = gr.Image(label="Next Latents", height=200, visible=False)
            result_video = gr.Video(label="Finished Frames", autoplay=True, show_share_button=False, height=512, loop=True)
            gr.Markdown('Note that the ending actions will be generated before the starting actions due to the inverted sampling. If the starting action is not in the video, you just need to wait, and it will be generated later.')
            progress_desc = gr.Markdown('', elem_classes='no-generating-animation')
            progress_bar = gr.HTML('', elem_classes='no-generating-animation')

    gr.HTML('<div style="text-align:center; margin-top:20px;">Share your results and find ideas at the <a href="https://x.com/search?q=framepack&f=live" target="_blank">FramePack Twitter (X) thread</a></div>')

    ips = [input_image, prompt_1, prompt_2, prompt_3, prompt_4, prompt_5, n_prompt, seed, total_second_length, latent_window_size, steps, cfg, gs, rs, gpu_memory_preservation, use_teacache, mp4_crf]
    start_button.click(fn=process, inputs=ips, outputs=[result_video, preview_image, progress_desc, progress_bar, start_button, end_button])
    end_button.click(fn=end_process)


block.launch(
    server_name=args.server,
    server_port=args.port,
    share=args.share,
    inbrowser=args.inbrowser,
)
