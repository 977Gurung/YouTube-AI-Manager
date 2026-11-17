"""Build a tiny random-weight LTX-Video image-to-video pipeline on disk (no downloads), plus a
single-file checkpoint is NOT produced: the sandbox exercises the repo-folder ("base") code path."""
import string
import sys
from pathlib import Path

import torch
from diffusers import AutoencoderKLLTXVideo, FlowMatchEulerDiscreteScheduler, LTXImageToVideoPipeline, LTXVideoTransformer3DModel
from tokenizers import Tokenizer, models
from transformers import T5Config, T5EncoderModel, T5TokenizerFast

out = Path(sys.argv[1])
pieces = ["<pad>", "</s>", "<unk>", "\u2581"] + list(string.ascii_lowercase + string.digits + ",.-:'") + ["\u2581" + c for c in string.ascii_lowercase]
vocab = {p: i for i, p in enumerate(pieces)}
tk = Tokenizer(models.Unigram([(p, -float(i) / 10) for i, p in enumerate(pieces)], unk_id=2, byte_fallback=False))
tok = T5TokenizerFast(tokenizer_object=tk, pad_token="<pad>", eos_token="</s>", unk_token="<unk>", model_max_length=128, extra_ids=0)

torch.manual_seed(0)
transformer = LTXVideoTransformer3DModel(in_channels=8, out_channels=8, patch_size=1, patch_size_t=1, num_attention_heads=4,
                                         attention_head_dim=8, cross_attention_dim=32, num_layers=1, caption_channels=32)
vae = AutoencoderKLLTXVideo(in_channels=3, out_channels=3, latent_channels=8, block_out_channels=(8, 8, 8, 8),
                            decoder_block_out_channels=(8, 8, 8, 8), layers_per_block=(1, 1, 1, 1, 1),
                            decoder_layers_per_block=(1, 1, 1, 1, 1), spatio_temporal_scaling=(True, True, False, False),
                            decoder_spatio_temporal_scaling=(True, True, False, False), decoder_inject_noise=(False,) * 5,
                            upsample_residual=(False,) * 4, upsample_factor=(1, 1, 1, 1), timestep_conditioning=False,
                            patch_size=1, patch_size_t=1, encoder_causal=True, decoder_causal=False)
vae.use_framewise_encoding = False
vae.use_framewise_decoding = False
te = T5EncoderModel(T5Config(vocab_size=len(vocab) + 8, d_model=32, d_kv=8, d_ff=37, num_layers=2, num_heads=4,
                             relative_attention_num_buckets=8, dropout_rate=0.0))
pipe = LTXImageToVideoPipeline(scheduler=FlowMatchEulerDiscreteScheduler(), vae=vae, text_encoder=te, tokenizer=tok, transformer=transformer)
pipe.save_pretrained(out, safe_serialization=True)
print("saved", out, "| vae ratios", vae.spatial_compression_ratio, vae.temporal_compression_ratio)
