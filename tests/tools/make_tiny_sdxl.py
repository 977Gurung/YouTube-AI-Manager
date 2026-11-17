"""Build a tiny random-weight SDXL pipeline on disk (no downloads) to exercise the imagegen code path."""
import json, string, sys
from pathlib import Path
import torch
from diffusers import AutoencoderKL, EulerDiscreteScheduler, StableDiffusionXLPipeline, UNet2DConditionModel
from transformers import CLIPTextConfig, CLIPTextModel, CLIPTextModelWithProjection, CLIPTokenizer

out = Path(sys.argv[1]); tok_dir = out.parent / "tok"; tok_dir.mkdir(parents=True, exist_ok=True)
vocab = {"<|startoftext|>": 0, "<|endoftext|>": 1}
for c in string.ascii_lowercase + string.digits + ",.-:'":
    vocab.setdefault(c, len(vocab)); vocab.setdefault(c + "</w>", len(vocab))
(tok_dir / "vocab.json").write_text(json.dumps(vocab)); (tok_dir / "merges.txt").write_text("#version: 0.2\n")
tok = CLIPTokenizer(str(tok_dir / "vocab.json"), str(tok_dir / "merges.txt"), pad_token="<|endoftext|>", model_max_length=77)
torch.manual_seed(0)
unet = UNet2DConditionModel(block_out_channels=(32, 64), layers_per_block=2, sample_size=32, in_channels=4, out_channels=4,
    down_block_types=("DownBlock2D", "CrossAttnDownBlock2D"), up_block_types=("CrossAttnUpBlock2D", "UpBlock2D"),
    attention_head_dim=(2, 4), use_linear_projection=True, addition_embed_type="text_time", addition_time_embed_dim=8,
    transformer_layers_per_block=(1, 2), projection_class_embeddings_input_dim=80, cross_attention_dim=64, norm_num_groups=1)
sched = EulerDiscreteScheduler(beta_start=0.00085, beta_end=0.012, steps_offset=1, beta_schedule="scaled_linear", timestep_spacing="leading")
vae = AutoencoderKL(block_out_channels=[32, 64], in_channels=3, out_channels=3, down_block_types=["DownEncoderBlock2D"] * 2,
    up_block_types=["UpDecoderBlock2D"] * 2, latent_channels=4, sample_size=128)
cfg = CLIPTextConfig(bos_token_id=0, eos_token_id=1, hidden_size=32, intermediate_size=37, layer_norm_eps=1e-05, num_attention_heads=4,
    num_hidden_layers=5, pad_token_id=1, vocab_size=len(vocab) + 10, hidden_act="gelu", projection_dim=32)
pipe = StableDiffusionXLPipeline(vae=vae, text_encoder=CLIPTextModel(cfg), text_encoder_2=CLIPTextModelWithProjection(cfg),
    tokenizer=tok, tokenizer_2=tok, unet=unet, scheduler=sched)
pipe.save_pretrained(out, safe_serialization=True)
print("saved", out)
