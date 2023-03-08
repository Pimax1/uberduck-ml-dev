import tempfile
import csv
from io import BytesIO

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from scipy.io import wavfile
import wandb
from torch.cuda.amp import autocast, GradScaler
from torch.optim.lr_scheduler import ExponentialLR

import ray
from ray.air import session, Checkpoint
from ray.air.config import ScalingConfig, RunConfig
from ray.air.integrations.wandb import  setup_wandb
import ray.data
# from ray.data.datasource import FastFileMetadataProvider
import ray.train as train
from ray.train.torch import TorchTrainer, TorchCheckpoint
from ray.tune import SyncConfig


# from uberduck_ml_dev.models import vits
# from uberduck_ml_dev.models.vits import *
from uberduck_ml_dev.models.radtts import RADTTS
from uberduck_ml_dev.text.symbols import NVIDIA_TACO2_SYMBOLS, SYMBOL_SETS
from uberduck_ml_dev.models.tacotron2 import DEFAULTS
from uberduck_ml_dev.text.utils import text_to_sequence
from uberduck_ml_dev.text.symbols import NVIDIA_TACO2_SYMBOLS
from uberduck_ml_dev.models.common import (
    spectrogram_torch,
)
from uberduck_ml_dev.losses import RADTTSLoss, AttentionBinarizationLoss

from uberduck_ml_dev.utils.utils import (
    intersperse,
    to_gpu,
    clip_grad_value_,
)

def _fix_state_dict(sd):
    return {k[7:]: v for k, v in sd.items()}


def _load_checkpoint_dict():
    checkpoint = session.get_checkpoint()
    if checkpoint is None:
        return
    checkpoint_dict = checkpoint.to_dict()
    return checkpoint_dict


# MODEL_CONFIG = {
#     "inter_channels": 192,
#     "hidden_channels": 192,
#     "filter_channels": 768,
#     "n_heads": 2,
#     "n_layers": 6,
#     "kernel_size": 3,
#     "p_dropout": 0.1,
#     "resblock": "1",
#     "resblock_kernel_sizes": [3, 7, 11],
#     "resblock_dilation_sizes": [[1, 3, 5], [1, 3, 5], [1, 3, 5]],
#     "upsample_rates": [8, 8, 2, 2],
#     "upsample_initial_channel": 512,
#     "upsample_kernel_sizes": [16, 16, 4, 4],
#     "n_layers_q": 3,
#     "use_spectral_norm": False,
# }
# DATA_CONFIG = {
#     "training_files": "filelists/ljs_audio_text_train_filelist.txt.cleaned",
#     "validation_files": "filelists/ljs_audio_text_val_filelist.txt.cleaned",
#     "text_cleaners": ["english_cleaners2"],
#     "max_wav_value": 32768.0,
#     "sampling_rate": 22050,
#     "filter_length": 1024,
#     "hop_length": 256,
#     "win_length": 1024,
#     "n_mel_channels": 80,
#     "mel_fmin": 0.0,
#     "mel_fmax": None,
#     "add_blank": True,
#     "n_speakers": 0,
#     "cleaned_text": True,
# }

# TRAIN_CONFIG = {
#     "log_interval": 200,
#     "eval_interval": 1000,
#     "seed": 1234,
#     "epochs": 20000,
#     "learning_rate": 2e-4,
#     "betas": [0.8, 0.99],
#     "eps": 1e-9,
#     "batch_size": 64,
#     "fp16_run": True,
#     "lr_decay": 0.999875,
#     "segment_size": 8192,
#     "init_lr_ratio": 1,
#     "warmup_epochs": 0,
#     "c_mel": 45,
#     "c_kl": 1.0,
# }

# config = DEFAULTS.values()
# config["with_gsts"] = False
config = {
    "train_config": {
        "output_directory": "/lj_minimal_decoder",
        "epochs": 10000000,
        "optim_algo": "RAdam",
        "learning_rate": 1e-4,
        "weight_decay": 1e-6,
        "sigma": 1.0,
        "iters_per_checkpoint": 2500,
        "steps_per_sample": 200,
        "batch_size": 6,
        "seed": None,
        "checkpoint_path": "",
        "ignore_layers": [],
        "ignore_layers_warmstart": [],
        "finetune_layers": [],
        "include_layers": [],
        "vocoder_config_path": "/usr/src/app/radtts/models/hifigan_22khz_config.json",
        "vocoder_checkpoint_path": "/usr/src/app/radtts/models/hifigan_libritts100360_generator0p5.pt",
        "log_attribute_samples": False,
        "log_decoder_samples": True,
        "warmstart_checkpoint_path": "",
        "use_amp": False,
        "grad_clip_val": 1.0,
        "loss_weights": {
            "blank_logprob": -1,
            "ctc_loss_weight": 0.1,
            "binarization_loss_weight": 1.0,
            "dur_loss_weight": 1.0,
            "f0_loss_weight": 1.0,
            "energy_loss_weight": 1.0,
            "vpred_loss_weight": 1.0
        },
        "binarization_start_iter": 6000,
        "kl_loss_start_iter": 18000,
        "unfreeze_modules": "all"
    },
    "data_config": {
        "training_files": {
            "LJS": {
                "basedir": "/",
                "audiodir": "/usr/src/app/radtts/data/lj_data/LJSpeech-1.1/wavs",
                "filelist": "/usr/src/app/radtts/data/lj_data/LJSpeech-1.1/metadata_formatted.txt",
                "lmdbpath": ""
            }
        },
        "validation_files": {
            "LJS": {
                "basedir": "/",
                "audiodir": "/usr/src/app/radtts/data/lj_data/LJSpeech-1.1/wavs",
                "filelist": "/usr/src/app/radtts/data/lj_data/LJSpeech-1.1/metadata_formatted.txt",
                "lmdbpath": ""
            }
        },
        "dur_min": 0.1,
        "dur_max": 10.2,
        "sampling_rate": 22050,
        "filter_length": 1024,
        "hop_length": 256,
        "win_length": 1024,
        "n_mel_channels": 80,
        "mel_fmin": 0.0,
        "mel_fmax": 8000.0,
        "f0_min": 80.0,
        "f0_max": 640.0,
        "max_wav_value": 32768.0,
        "use_f0": True,
        "use_log_f0": 0,
        "use_energy_avg": True,
        "use_scaled_energy": True,
        "symbol_set": "radtts",
        "cleaner_names": ["radtts_cleaners"],
        "heteronyms_path": "tts_text_processing/heteronyms",
        "phoneme_dict_path": "tts_text_processing/cmudict-0.7b",
        "p_phoneme": 1.0,
        "handle_phoneme": "word",
        "handle_phoneme_ambiguous": "ignore",
        "include_speakers": None,
        "n_frames": -1,
        "betabinom_cache_path": "data_cache/",
        "lmdb_cache_path": "", 
        "use_attn_prior_masking": True,
        "prepend_space_to_text": True,
        "append_space_to_text": True,
        "add_bos_eos_to_text": False,
        "betabinom_scaling_factor": 1.0,
        "distance_tx_unvoiced": False,
        "mel_noise_scale": 0.0
    },
    "model_config": {
        "n_speakers": 1,
        "n_speaker_dim": 16,
        "n_text": 185,
        "n_text_dim": 512,
        "n_flows": 8,
        "n_conv_layers_per_step": 4,
        "n_mel_channels": 80,
        "n_hidden": 1024,
        "mel_encoder_n_hidden": 512,
        "dummy_speaker_embedding": False,
        "n_early_size": 2,
        "n_early_every": 2,
        "n_group_size": 2,
        "affine_model": "wavenet",
        "include_modules": "decatnvpred",
        "scaling_fn": "tanh",
        "matrix_decomposition": "LUS",
        "learn_alignments": True,
        "use_speaker_emb_for_alignment": False,
        "attn_straight_through_estimator": True,
        "use_context_lstm": True,
        "context_lstm_norm": "spectral",
        "context_lstm_w_f0_and_energy": True,
        "text_encoder_lstm_norm": "spectral",
        "n_f0_dims": 1,
        "n_energy_avg_dims": 1,
        "use_first_order_features": False,
        "unvoiced_bias_activation": "relu",
        "decoder_use_partial_padding": True,
        "decoder_use_unvoiced_bias": True,
        "ap_pred_log_f0": True,
        "ap_use_unvoiced_bias": True,
        "ap_use_voiced_embeddings": True,
        "dur_model_config": None,
        "f0_model_config": None,
        "energy_model_config": None,
        "v_model_config": {
            "name": "dap",
            "hparams": {
                "n_speaker_dim": 16,
                "take_log_of_input": False,
                "bottleneck_hparams": {
                    "in_dim": 512,
                    "reduction_factor": 16,
                    "norm": "weightnorm",
                    "non_linearity": "relu"
                },
                "arch_hparams": {
                    "out_dim": 1,
                    "n_layers": 2,
                    "n_channels": 256,
                    "kernel_size": 3,
                    "p_dropout": 0.5,
                    "lstm_type": "",
                    "use_linear": 1
                }
            }
        }
    }
}

train_config = config['train_config']
model_config = config['model_config']
data_config = config['data_config']

@torch.no_grad()
def sample_inference(model):
    # sample_text = random_utterance()
    # text_sequence = torch.LongTensor(
    #     intersperse(
    #         text_to_sequence(
    #             sample_text,
    #             ["english_cleaners"],
    #             1.0,
    #             symbol_set=NVIDIA_TACO2_SYMBOLS,
    #         ),
    #         0,
    #     )
    # ).unsqueeze(0)

    # audio, *_ = model.infer(
    #     text_sequence, text_lengths
    # )
    # audio = audio.data.squeeze().cpu().numpy()
    # return audio

    return None

class DataCollate():
    """ Zero-pads model inputs and targets given number of steps """
    def __init__(self, n_frames_per_step=1):
        self.n_frames_per_step = n_frames_per_step

    def __call__(self, batch):
        """Collate from normalized data """
        # Right zero-pad all one-hot text sequences to max input length
        input_lengths, ids_sorted_decreasing = torch.sort(
            torch.LongTensor([len(x['text_encoded']) for x in batch]),
            dim=0, descending=True)

        max_input_len = input_lengths[0]
        text_padded = torch.LongTensor(len(batch), max_input_len)
        text_padded.zero_()

        for i in range(len(ids_sorted_decreasing)):
            text = batch[ids_sorted_decreasing[i]]['text_encoded']
            text_padded[i, :text.size(0)] = text

        # Right zero-pad mel-spec
        num_mel_channels = batch[0]['mel'].size(0)
        max_target_len = max([x['mel'].size(1) for x in batch])

        # include mel padded, gate padded and speaker ids
        mel_padded = torch.FloatTensor(len(batch), num_mel_channels, max_target_len)
        mel_padded.zero_()
        f0_padded = None
        p_voiced_padded = None
        voiced_mask_padded = None
        energy_avg_padded = None
        if batch[0]['f0'] is not None:
            f0_padded = torch.FloatTensor(len(batch), max_target_len)
            f0_padded.zero_()

        if batch[0]['p_voiced'] is not None:
            p_voiced_padded = torch.FloatTensor(len(batch), max_target_len)
            p_voiced_padded.zero_()

        if batch[0]['voiced_mask'] is not None:
            voiced_mask_padded = torch.FloatTensor(len(batch), max_target_len)
            voiced_mask_padded.zero_()

        if batch[0]['energy_avg'] is not None:
            energy_avg_padded = torch.FloatTensor(len(batch), max_target_len)
            energy_avg_padded.zero_()

        attn_prior_padded = torch.FloatTensor(len(batch), max_target_len, max_input_len)
        attn_prior_padded.zero_()

        output_lengths = torch.LongTensor(len(batch))
        speaker_ids = torch.LongTensor(len(batch))
        audiopaths = []
        for i in range(len(ids_sorted_decreasing)):
            mel = batch[ids_sorted_decreasing[i]]['mel']
            mel_padded[i, :, :mel.size(1)] = mel
            if batch[ids_sorted_decreasing[i]]['f0'] is not None:
                f0 = batch[ids_sorted_decreasing[i]]['f0']
                # print(f0.shape, f0_padded.shape)
                f0_padded[i, :len(f0)] = f0

            if batch[ids_sorted_decreasing[i]]['voiced_mask'] is not None:
                voiced_mask = batch[ids_sorted_decreasing[i]]['voiced_mask']
                voiced_mask_padded[i, :len(f0)] = voiced_mask

            if batch[ids_sorted_decreasing[i]]['p_voiced'] is not None:
                p_voiced = batch[ids_sorted_decreasing[i]]['p_voiced']
                p_voiced_padded[i, :len(f0)] = p_voiced

            if batch[ids_sorted_decreasing[i]]['energy_avg'] is not None:
                energy_avg = batch[ids_sorted_decreasing[i]]['energy_avg']
                energy_avg_padded[i, :len(energy_avg)] = energy_avg

            output_lengths[i] = mel.size(1)
            speaker_ids[i] = batch[ids_sorted_decreasing[i]]['speaker_id']
            audiopath = 'whocares'#batch[ids_sorted_decreasing[i]]['audiopath']
            audiopaths.append(audiopath)
            cur_attn_prior = batch[ids_sorted_decreasing[i]]['attn_prior']
            if cur_attn_prior is None:
                attn_prior_padded = None
            else:
                attn_prior_padded[i, :cur_attn_prior.size(0), :cur_attn_prior.size(1)] = cur_attn_prior

        return {'mel': mel_padded,
                'speaker_ids': speaker_ids,
                'text': text_padded,
                'input_lengths': input_lengths,
                'output_lengths': output_lengths,
                'audiopaths': audiopaths,
                'attn_prior': attn_prior_padded,
                'f0': f0_padded,
                'p_voiced': p_voiced_padded,
                'voiced_mask': voiced_mask_padded,
                'energy_avg': energy_avg_padded
                }

max_wav_value = 32768
from librosa import pyin
def get_f0_pvoiced(audio, sampling_rate=22050, frame_length=1024,
                    hop_length=256, f0_min=100, f0_max=300):

    audio_norm = audio / max_wav_value
    f0, voiced_mask, p_voiced = pyin(
        y = audio_norm, fmin = f0_min, fmax = f0_max, sr = sampling_rate,
        frame_length=frame_length, win_length=frame_length // 2,
        hop_length=hop_length)
    f0[~voiced_mask] = 0.0
    f0 = torch.FloatTensor(f0)
    p_voiced = torch.FloatTensor(p_voiced)
    voiced_mask = torch.FloatTensor(voiced_mask)
    return f0, voiced_mask, p_voiced


    
use_scaled_energy = True
def energy_avg_normalize(x):
    if  use_scaled_energy == True:
        x = (x + 20.0) / 20.0
    return x
    
def get_energy_average(mel):
    energy_avg = mel.mean(0)
    energy_avg = energy_avg_normalize(energy_avg)
    return energy_avg

import os
import pickle as pkl
from scipy.stats import betabinom

def beta_binomial_prior_distribution(phoneme_count, mel_count, scaling_factor=0.05):
    P = phoneme_count
    M = mel_count
    x = np.arange(0, P)
    mel_text_probs = []
    for i in range(1, M + 1):
        a, b = scaling_factor * i, scaling_factor * (M + 1 - i)
        rv = betabinom(P - 1, a, b)
        mel_i_prob = rv.pmf(x)
        mel_text_probs.append(mel_i_prob)
    return torch.tensor(np.array(mel_text_probs))


def get_attention_prior(n_tokens, n_frames):
    # cache the entire attn_prior by filename
    # if self.use_attn_prior_masking:
    filename = "{}_{}".format(n_tokens, n_frames)
    betabinom_cache_path = 'yourmom'
    if not os.path.exists(betabinom_cache_path):
        os.makedirs(betabinom_cache_path, exist_ok=False)
    prior_path = os.path.join(betabinom_cache_path, filename)
    prior_path += "_prior.pth"
    # if self.lmdb_cache_path != "":
    #     attn_prior = pkl.loads(
    #         self.cache_data_lmdb.get(prior_path.encode("ascii"))
    #     )
    if os.path.exists(prior_path):
        attn_prior = torch.load(prior_path)
    else:
        attn_prior = beta_binomial_prior_distribution(
            n_tokens, n_frames, scaling_factor = 0.05
        )
        torch.save(attn_prior, prior_path)
    # else:
        # attn_prior = torch.ones(n_frames, n_tokens)  # all ones baseline

    return attn_prior

def f0_normalize( x, f0_min):
    # if self.use_log_f0:
    # mask = x >= f0_min
    # x[mask] = torch.log(x[mask])
    # x[~mask] = 0.0

    return x
    
from uberduck_ml_dev.data.audio_processing import TacotronSTFT
stft = TacotronSTFT(
    filter_length=data_config['filter_length'],
    hop_length=data_config['hop_length'],
    win_length=data_config['win_length'],
    sampling_rate=22050,
    n_mel_channels=data_config['n_mel_channels'],
    mel_fmin=data_config['mel_fmin'],
    mel_fmax=data_config['mel_fmax'],
)

def get_speaker_id(speaker):

    return torch.LongTensor([speaker])

from uberduck_ml_dev.models.common import get_mel


def ray_df_to_batch_radtts(df):
    transcripts = df.transcript.tolist()
    audio_bytes_list = df.audio_bytes.tolist()
    speaker_ids = df.speaker_id.tolist()
    collate_fn = DataCollate()
    collate_input = []
    for transcript, audio_bytes, speaker_id in zip(
        transcripts, audio_bytes_list, speaker_ids
    ):
        # Audio
        bio = BytesIO(audio_bytes)
        sr, wav_data = wavfile.read(bio)
        audio = torch.FloatTensor(wav_data)
        audio_norm = audio / (np.abs(audio).max() * 2)
        # audio_norm = audio_norm.unsqueeze(0)
        # Text
        text_sequence = torch.LongTensor(
            intersperse(
                text_to_sequence(
                    transcript,
                    ["english_cleaners"],
                    1.0,
                    symbol_set=NVIDIA_TACO2_SYMBOLS,
                ),
                0,
            )
        )
        # Spectrogram

        mel = get_mel(audio_norm, data_config['max_wav_value'], stft)
        mel = torch.squeeze(mel, 0)
        f0, voiced_mask, p_voiced = get_f0_pvoiced(
            audio.cpu().numpy(), f0_min = data_config['f0_min'], f0_max=data_config["f0_max"], hop_length=data_config['hop_length'], frame_length=data_config['filter_length'], sampling_rate=22050)   
        f0 = f0_normalize(f0, f0_min = data_config['f0_min'])
        energy_avg = get_energy_average(mel)
        attn_prior = get_attention_prior(text_sequence.shape[0], mel.shape[1])

        speaker_id =  get_speaker_id(speaker_id)
        # print(type(mel))
        # print(type(text_sequence), type (speaker_id), type(f0), type (p_voiced), type(voiced_mask), type(energy_avg), type(attn_prior))
        collate_input.append({'text_encoded': text_sequence, 'mel':mel, 'speaker_id':speaker_id, 'f0': f0, 'p_voiced' : p_voiced, 'voiced_mask': voiced_mask, 'energy_avg': energy_avg, 'attn_prior' : attn_prior})
    return collate_fn(collate_input)



def get_ray_dataset():
    lj_df = pd.read_csv(
        "https://uberduck-datasets-dirty.s3.us-west-2.amazonaws.com/lj_for_upload/metadata_formatted_100_edited.txt",
        sep="|",
        header=None,
        quoting=3,
        names=["path", "transcript", "speaker_id"], # pitch path is implicit - this should be changed
    )

    paths = lj_df.path.tolist()
    transcripts = lj_df.transcript.tolist()
    speaker_ids = lj_df.speaker_id.tolist()

    parallelism_length = 400
    audio_ds = ray.data.read_binary_files(
        paths,
        parallelism=parallelism_length,
        # meta_provider=FastFileMetadataProvider(),
        ray_remote_args={"num_cpus": 0.2},
    )
    audio_ds = audio_ds.map_batches(
        lambda x: x, batch_format="pyarrow", batch_size=None
    )

    paths = ray.data.from_items(paths, parallelism=parallelism_length)
    paths_ds = paths.map_batches(lambda x: x, batch_format="pyarrow", batch_size=None)

    transcripts = ray.data.from_items(transcripts, parallelism=parallelism_length)
    transcripts_ds = transcripts.map_batches(lambda x: x, batch_format="pyarrow", batch_size=None)

    speaker_ids_ds = ray.data.from_items(speaker_ids, parallelism=parallelism_length)
    speaker_ids_ds = speaker_ids_ds.map_batches(
        lambda x: x, batch_format="pyarrow", batch_size=None
    )
    output_dataset = (
        transcripts_ds.zip(audio_ds)
        .zip(paths_ds)
        .zip(speaker_ids_ds)
    )
    output_dataset = output_dataset.map_batches(
        lambda table: table.rename(
            columns={
                "value": "transcript",
                "value_1": "audio_bytes",
                "value_2": "path",
                "value_3": "speaker_id"
            }
        )
    )
    return output_dataset



@torch.no_grad()
def log(metrics, gen_audio=None, gt_audio=None, sample_audio=None):
    pass
    # wandb_metrics = dict(metrics)
    # if gen_audio is not None:
    #     wandb_metrics.update({"gen/audio": wandb.Audio(gen_audio, sample_rate=22050)})
    # if gt_audio is not None:
    #     wandb_metrics.update({"gt/audio": wandb.Audio(gt_audio, sample_rate=22050)})
    # if sample_audio is not None:
    #     wandb_metrics.update(
    #         {"sample_inference": wandb.Audio(sample_audio, sample_rate=22050)}
    #     )
    # session.report(metrics)
    # if session.get_world_rank() == 0:
    #     wandb.log(wandb_metrics)


def _train_step(
    batch,
    model,
    optim,
    global_step,
    steps_per_sample,
    scaler,
    scheduler,
    criterion,
    attention_kl_loss,
    iteration,
    kl_loss_start_iter,
    binarization_start_iter
):

    if iteration >= binarization_start_iter:
        binarize = True
    else:
        binarize = False

    optim.zero_grad()
    # Transform ray_batch_df to (x, x_lengths, spec, spec_lengths, y, y_lengths)
    with autocast(enabled= False):
        batch_dict = ray_df_to_batch_radtts(batch)
        mel = to_gpu(batch_dict['mel'])
        speaker_ids = to_gpu(batch_dict['speaker_ids'])
        attn_prior = to_gpu(batch_dict['attn_prior'])
        f0 = to_gpu(batch_dict['f0'])
        voiced_mask = to_gpu(batch_dict['voiced_mask'])
        p_voiced = to_gpu(batch_dict['p_voiced'])
        text = to_gpu(batch_dict['text'])
        in_lens = to_gpu(batch_dict['input_lengths'])
        out_lens = to_gpu(batch_dict['output_lengths'])
        energy_avg = to_gpu(batch_dict['energy_avg'])

        outputs = model(
                    mel, speaker_ids, text, in_lens, out_lens,
                    binarize_attention=binarize, attn_prior=attn_prior,
                    f0=f0, energy_avg=energy_avg,
                    voiced_mask=voiced_mask, p_voiced=p_voiced)

        loss_outputs = criterion(outputs, in_lens, out_lens)

        loss = None
        for k, (v, w) in loss_outputs.items():
            if w > 0:
                loss = v * w if loss is None else loss + v * w

        w_bin = criterion.loss_weights.get('binarization_loss_weight', 1.0)
        if binarize and iteration >= kl_loss_start_iter:
            binarization_loss = attention_kl_loss(
                outputs['attn'], outputs['attn_soft'])
            loss += binarization_loss * w_bin
        else:
            binarization_loss = torch.zeros_like(loss)
        loss_outputs['binarization_loss'] = (binarization_loss, w_bin)

 
    optim.zero_grad()
    scaler.scale(loss).backward()
    scaler.unscale_(optim)
    clip_grad_value_(model.parameters(), 100)
    scaler.step(optim)
    scaler.update()
    scheduler.step()


    # metrics = loss_outputs.items() # add loss
    # if global_step % steps_per_sample == 0 and session.get_world_rank() == 0:
    #     gen_audio = None#y_hat[0][0][: y_lengths[0]].data.cpu().numpy().astype("float32")
    #     gt_audio = None#y[0][0][: y_lengths[0]].data.cpu().numpy().astype("float32")
    #     model.eval()
    #     sample_audio = sample_inference(
    #         model
    #     )
    #     model.train()

    #     # log(metrics, gen_audio, gt_audio, sample_audio)
    # else:
    #     # log(metrics)
    #     pass
    print(f"Loss: {loss.item()}")


def train_func(config: dict):
    setup_wandb(config, project="radtts-ray", rank_zero_only=False)
    print("CUDA AVAILABLE: ", torch.cuda.is_available())
    epochs = config["epochs"]
    batch_size = config["batch_size"]
    steps_per_sample = config["steps_per_sample"]
    # gin_channels = config["gin_channels"]
    sigma = config['sigma']
    kl_loss_start_iter = config['kl_loss_start_iter']
    binarization_start_iter = config['binarization_start_iter']

    model = RADTTS(
        **model_config,
    )
    model = train.torch.prepare_model(model, parallel_strategy_kwargs = dict(find_unused_parameters=True))

    checkpoint_dict = _load_checkpoint_dict()
    if checkpoint_dict is None:
        global_step = 0
        start_epoch = 0
    else:
        global_step = checkpoint_dict["global_step"]
        start_epoch = checkpoint_dict["epoch"]
        if session.get_world_size() > 1:
            model_sd = checkpoint_dict["model"]
            model.load_state_dict(model_sd)
        else:
            model_sd = _fix_state_dict(checkpoint_dict["model"])
            model.load_state_dict(model_sd)
        del checkpoint_dict

    # NOTE (Sam): replace with RAdam
    optim = torch.optim.Adam(
        model.parameters(),
        lr = config["learning_rate"],
        weight_decay = config["weight_decay"]
    )
    scheduler = ExponentialLR(
        optim,
        config["weight_decay"],
        last_epoch=-1,
    )
    dataset_shard = session.get_dataset_shard("train")
    global_step = 0
    scaler = GradScaler()

    criterion = RADTTSLoss(
        sigma,
        config['n_group_size'],
        config['dur_model_config'],
        config['f0_model_config'],
        config['energy_model_config'],
        vpred_model_config=config['v_model_config'],
        loss_weights=config['loss_weights']
    )
    attention_kl_loss = AttentionBinarizationLoss()
    iteration = 0
    for epoch in range(start_epoch, start_epoch + epochs):
        for batch_idx, ray_batch_df in enumerate(
            dataset_shard.iter_batches(batch_size=batch_size)
        ):
            torch.cuda.empty_cache()
            _train_step(
                ray_batch_df,
                model,
                optim,
                global_step,
                steps_per_sample,
                scaler,
                scheduler,
                criterion,
                attention_kl_loss,
                iteration,
                kl_loss_start_iter,
                binarization_start_iter,
            )
            global_step += 1


from ray.train.torch import TorchTrainer, TorchCheckpoint, TorchTrainer
from ray.air.config import ScalingConfig, RunConfig
from ray.tune import SyncConfig

if __name__ == "__main__":

    ray_dataset = get_ray_dataset()
    train_config['n_group_size'] = model_config['n_group_size']
    train_config['dur_model_config'] = model_config['dur_model_config']
    train_config['f0_model_config'] = model_config['f0_model_config']
    train_config['energy_model_config'] = model_config['energy_model_config']
    train_config['v_model_config']=model_config['v_model_config']

    trainer = TorchTrainer(
        train_loop_per_worker=train_func,
        train_loop_config=train_config,
        scaling_config=ScalingConfig(
            num_workers=2, use_gpu=True, resources_per_worker=dict(CPU=4, GPU=1)
        ),
        run_config=RunConfig(
            sync_config=SyncConfig(upload_dir="s3://uberduck-anyscale-data/checkpoints")
        ),
        datasets={"train": ray_dataset},
    )

    result = trainer.fit()