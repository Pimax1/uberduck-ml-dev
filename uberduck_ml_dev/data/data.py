import torch
import os
from typing import List, Optional, Dict
from torch.utils.data import Dataset
from einops import rearrange
from scipy.io.wavfile import read
from scipy.ndimage import distance_transform_edt as distance_transform
import numpy as np
from librosa import pyin
import lmdb
import pickle as pkl


from ..models.common import MelSTFT
from ..utils.utils import (
    load_filepaths_and_text,
    intersperse,
)
from .utils import (
    oversample,
    _orig_to_dense_speaker_id,
    beta_binomial_prior_distribution,
)
from ..text.utils import text_to_sequence
from ..text.text_processing import TextProcessing
from ..models.common import (
    FILTER_LENGTH,
    HOP_LENGTH,
    WIN_LENGTH,
    SAMPLING_RATE,
    N_MEL_CHANNELS,
    TacotronSTFT,
)
from ..text.symbols import NVIDIA_TACO2_SYMBOLS

F0_MIN = 80
F0_MAX = 640


# NOTE (Sam): generic dataset class for all purposes avoids writing redundant methods (e.g. get pitch when text isn't available).
# However, functional factorization of this dataloader (e.g. get_mels) and merging classes as needed would be preferable.
# NOTE (Sam): "load" means load from file. "return" means return to collate. "get" is a functional element.  "has" and "with" are tbd equivalent in trainer/model.
class Data(Dataset):
    def __init__(
        self,
        audiopaths_and_text: Optional[
            List[str]
        ] = None,  # TODO (Sam): consider removing triplicate audiopaths_and_text argument
        oversample_weights=None,
        # Text parameters
        return_texts: bool = True,  # NOTE (Sam): maybe include include_texts parameter if text is ever inferred.
        texts: Optional[List[str]] = None,
        intersperse_text: Optional[bool] = False,
        intersperse_token: Optional[int] = 0,
        text_cleaners: Optional[List[str]] = ["english_cleaners"],
        p_arpabet: Optional[float] = 1.0,
        # Audio parameters
        return_mels=True,
        audiopaths: Optional[List[str]] = None,
        n_mel_channels: Optional[int] = N_MEL_CHANNELS,
        sampling_rate: Optional[int] = 22050,
        mel_fmin: Optional[float] = 0.0,
        mel_fmax: Optional[float] = 8000,
        filter_length: Optional[int] = FILTER_LENGTH,
        hop_length: Optional[int] = HOP_LENGTH,
        win_length: Optional[int] = WIN_LENGTH,
        symbol_set: Optional[str] = NVIDIA_TACO2_SYMBOLS,
        padding: Optional[int] = None,
        max_wav_value: Optional[float] = 32768.0,
        # Pitch parameters
        # TODO (Sam): consider use_f0 = load_f0 or compute_f0
        return_f0s: bool = False,
        f0_cache_path: Optional[str] = None,
        use_log_f0: Optional[bool] = True,
        load_f0s: bool = False,
        f0_min: Optional[int] = F0_MIN,
        f0_max: Optional[int] = F0_MAX,
        # Torchmoji parameters
        return_gsts: bool = False,
        load_gsts=False,  # TODO (Sam): check this against existing crust models
        get_gst=lambda text: None,  # NOTE (Sam): this is a functional argument.
        # Speaker embedding parameters
        return_speaker_ids: bool = True,
        load_speaker_ids: bool = True,
        speaker_ids: Optional[List[str]] = None,
        # TODO (Sam): extend include/compute syntax to these embeddings.
        audio_encoder_forward=None,
        speaker_embeddings=None,
        # Control parameters
        debug: bool = False,
        debug_dataset_size: int = None,
    ):
        super().__init__()
        self.debug = debug
        self.debug_dataset_size = debug_dataset_size

        # TODO (Sam): refactor support for oversampling to make generic across data types.
        # NOTE (Sam): right now only old audiopaths_and_text based loading is supported for training.
        if audiopaths_and_text:
            oversample_weights = {}
            self.audiopaths_and_text = oversample(
                load_filepaths_and_text(audiopaths_and_text), oversample_weights
            )
        if hasattr(self, "audiopaths_and_text"):
            self.audiopaths = [i[0] for i in self.audiopaths_and_text]
        else:
            self.audiopaths = audiopaths

        # TODO (Sam): make this assignment automatic
        self.return_texts = return_texts
        self.return_mels = return_mels
        self.return_f0s = return_f0s
        self.f0_cache_path = f0_cache_path
        self.load_f0s = load_f0s
        self.return_gsts = return_gsts
        self.load_gsts = load_gsts
        self.return_speaker_ids = return_speaker_ids
        self.load_speaker_ids = load_speaker_ids
        # NOTE (Sam): these are parameters for both audio loading and inference.
        self.sampling_rate = sampling_rate
        self.filter_length = filter_length
        self.hop_length = hop_length
        self.max_wav_value = max_wav_value
        self.f0_min = f0_min
        self.f0_max = f0_max
        self.use_log_f0 = use_log_f0

        if self.return_mels:
            self.stft = MelSTFT(
                filter_length=filter_length,
                hop_length=hop_length,
                win_length=win_length,
                n_mel_channels=n_mel_channels,
                sampling_rate=sampling_rate,
                mel_fmin=mel_fmin,
                mel_fmax=mel_fmax,
                padding=padding,
            )
            self.max_wav_value = max_wav_value

            self.mel_fmin = mel_fmin
            self.mel_fmax = mel_fmax

        if self.return_texts:
            self.text_cleaners = text_cleaners
            self.p_arpabet = p_arpabet
            self.symbol_set = symbol_set
            self.intersperse_text = intersperse_text
            self.intersperse_token = intersperse_token
            # NOTE (Sam): this could be moved outside of return text if text statistics analogous to text as f0 is to audio are computed.
            if hasattr(self, "audiopaths_and_text"):
                self.texts = [i[1] for i in self.audiopaths_and_text]
            else:
                self.texts = texts

        if self.return_gsts and not self.load_gsts:
            self.get_gst = get_gst

        if self.return_f0s:
            os.makedirs(self.f0_cache_path, exist_ok=True)

        if self.return_speaker_ids:
            if self.load_speaker_ids:
                if hasattr(self, "audiopaths_and_text"):
                    speaker_ids = [i[2] for i in self.audiopaths_and_text]
                    self._speaker_id_map = _orig_to_dense_speaker_id(speaker_ids)
            # else could be speaker classification positions for example.

        # NOTE (Sam): this is hacky and not consistent with other approaches.
        self.audio_encoder_forward = audio_encoder_forward
        self.speaker_embeddings = speaker_embeddings

    # NOTE (Sam): this is the RADTTS version - more recent than mellotron from the same author.
    # NOTE (Sam): in contrast to get_gst, the computation here is kept in this file rather than a functional argument.
    def _get_f0(self, audiopath, audio):
        filename = "_".join(audiopath.split("/")).split(".wav")[0]
        f0_path = os.path.join(self.f0_cache_path, filename)
        f0_path += "_f0_sr{}_fl{}_hl{}_f0min{}_f0max{}_log{}.pt".format(
            self.sampling_rate,
            self.filter_length,
            self.hop_length,
            self.f0_min,
            self.f0_max,
            self.use_log_f0,
        )

        # NOTE (Sam): this is inhereted from RAD TTS and redundant with load_f0 syntax.
        dikt = None
        if os.path.exists(f0_path):
            try:
                dikt = torch.load(f0_path)
            except:
                print(f"f0 loading from {f0_path} is broken, recomputing.")

        if dikt is not None:
            f0 = dikt["f0"]
            p_voiced = dikt["p_voiced"]
            voiced_mask = dikt["voiced_mask"]
        else:
            f0, voiced_mask, p_voiced = self.get_f0_pvoiced(
                audio.cpu().numpy(),
                self.sampling_rate,
                self.filter_length,
                self.hop_length,
                self.f0_min,
                self.f0_max,
            )
            print("saving f0 to {}".format(f0_path))
            torch.save(
                {"f0": f0, "voiced_mask": voiced_mask, "p_voiced": p_voiced},
                f0_path,
            )
        if f0 is None:
            raise Exception("STOP, BROKEN F0 {}".format(audiopath))

        f0 = self.f0_normalize(f0)

        return f0

    def f0_normalize(self, x):
        if self.use_log_f0:
            mask = x >= self.f0_min
            x[mask] = torch.log(x[mask])
            x[~mask] = 0.0

        return x

    def _get_audio_encoding(self, audio):
        return self.audio_encoder_forward(audio)

    def _get_data(
        self,
        audiopath_and_text: Optional[List[str]] = None,
        audiopath: Optional[str] = None,
        text: Optional[str] = None,
        speaker_id: Optional[int] = None,
    ):
        data = {}
        if audiopath_and_text is not None:
            audiopath, text, speaker_id = audiopath_and_text
        if speaker_id is not None:
            speaker_id = self._speaker_id_map[speaker_id]

        if self.return_texts:
            text_sequence = torch.LongTensor(
                text_to_sequence(
                    text,
                    self.text_cleaners,
                    p_arpabet=self.p_arpabet,
                    symbol_set=self.symbol_set,
                )
            )
            if self.intersperse_text:
                text_sequence = torch.LongTensor(
                    intersperse(text_sequence.numpy(), self.intersperse_token)
                )  # add a blank token, whose id number is len(symbols)
            data["text_sequence"] = text_sequence

        if audiopath is not None:
            sampling_rate, wav_data = read(audiopath)
            # NOTE (Sam): is this the right normalization?  Should it be done here or in preprocessing.
            audio = torch.FloatTensor(wav_data)
            audio_norm = audio / (
                np.abs(audio).max() * 2
            )  # NOTE (Sam): just must be < 1.
            audio_norm = audio_norm.unsqueeze(0)

        if self.return_mels:
            melspec = self.stft.mel_spectrogram(audio_norm)
            melspec = torch.squeeze(melspec, 0)
            data["mel"] = melspec

        f0 = None
        if self.return_f0s:
            if not self.load_f0s:
                assert audiopath is not None
                f0 = self._get_f0(audiopath, audio_norm[0])
                data["f0"] = f0

        if self.return_speaker_ids:
            data["speaker_id"] = speaker_id

        if self.return_gsts:
            if not self.load_gsts:
                embedded_gst = self.get_gst([text])
                data["embedded_gst"] = embedded_gst

        if self.audio_encoder_forward is not None:
            # NOTE (Sam): hardcoded for now for single speaker.
            audio_encoding = rearrange(self.speaker_embeddings, "o s -> 1 o s")
            data["audio_encoding"] = audio_encoding

        return data

    def __getitem__(self, idx):
        """Return data for a single data point."""
        try:
            if hasattr(self, "audiopaths_and_text"):
                data = self._get_data(self.audiopaths_and_text[idx])
            # TODO (Sam): accomodate more options as needed.
            elif hasattr(self, "audiopaths"):
                data = self._get_data(
                    audiopath=self.audiopaths[idx],
                )
        except Exception as e:
            print(f"Error while getting data: index = {idx}")
            print(e)
            raise
        return data

    def __len__(self):
        if self.debug and self.debug_dataset_size:
            debug_dataset_size = self.debug_dataset_size
        # TODO (Sam): this is a bit unfinished. Assert equals for separate arguments of text and audio.
        nfiles = []
        if hasattr(self, "audiopaths_and_text"):
            nfiles.append(len(self.audiopaths_and_text))
        elif hasattr(self, "audiopaths"):
            nfiles.append(len(self.audiopaths))
        assert len(set(nfiles)) == 1, "All dataset sizes must be equal"
        nfiles = nfiles[0]
        if self.debug and self.debug_dataset_size:
            return min(debug_dataset_size, nfiles)
        return nfiles

    def sample_test_batch(self, size):
        idx = np.random.choice(range(len(self)), size=size, replace=False)
        test_batch = []
        for index in idx:
            test_batch.append(self.__getitem__(index))
        return test_batch

    def get_f0_pvoiced(
        self,
        audio,
        sampling_rate=SAMPLING_RATE,
        frame_length=FILTER_LENGTH,
        hop_length=HOP_LENGTH,
        f0_min=F0_MIN,
        f0_max=F0_MAX,
    ):
        f0, voiced_mask, p_voiced = pyin(
            audio,
            f0_min,
            f0_max,
            sampling_rate,
            frame_length=frame_length,
            win_length=frame_length // 2,
            hop_length=hop_length,
        )
        f0[~voiced_mask] = 0.0
        f0 = torch.FloatTensor(f0)
        p_voiced = torch.FloatTensor(p_voiced)
        voiced_mask = torch.FloatTensor(voiced_mask)
        return f0, voiced_mask, p_voiced


class DataRADTTS(torch.utils.data.Dataset):
    def __init__(
        self,
        datasets,
        filter_length,
        hop_length,
        win_length,
        sampling_rate,
        n_mel_channels,
        mel_fmin,
        mel_fmax,
        f0_min,
        f0_max,
        max_wav_value,
        use_f0,
        use_energy_avg,
        use_log_f0,
        use_scaled_energy,
        symbol_set,
        cleaner_names,
        heteronyms_path,
        phoneme_dict_path,
        p_phoneme,
        handle_phoneme="word",
        handle_phoneme_ambiguous="ignore",
        speaker_ids=None,
        include_speakers=None,
        n_frames=-1,
        use_attn_prior_masking=True,
        prepend_space_to_text=True,
        append_space_to_text=True,
        add_bos_eos_to_text=False,
        betabinom_cache_path="",
        betabinom_scaling_factor=0.05,
        lmdb_cache_path="",
        dur_min=None,
        dur_max=None,
        combine_speaker_and_emotion=False,
        **kwargs,
    ):
        self.combine_speaker_and_emotion = combine_speaker_and_emotion
        self.max_wav_value = max_wav_value
        self.audio_lmdb_dict = {}  # dictionary of lmdbs for audio data
        self.data = self.load_data(datasets)
        self.distance_tx_unvoiced = False
        if "distance_tx_unvoiced" in kwargs.keys():
            self.distance_tx_unvoiced = kwargs["distance_tx_unvoiced"]
        self.stft = TacotronSTFT(
            filter_length=filter_length,
            hop_length=hop_length,
            win_length=win_length,
            sampling_rate=sampling_rate,
            n_mel_channels=n_mel_channels,
            mel_fmin=mel_fmin,
            mel_fmax=mel_fmax,
        )

        self.do_mel_scaling = kwargs.get("do_mel_scaling", True)
        self.mel_noise_scale = kwargs.get("mel_noise_scale", 0.0)
        self.filter_length = filter_length
        self.hop_length = hop_length
        self.win_length = win_length
        self.mel_fmin = mel_fmin
        self.mel_fmax = mel_fmax
        self.f0_min = f0_min
        self.f0_max = f0_max
        self.use_f0 = use_f0
        self.use_log_f0 = use_log_f0
        self.use_energy_avg = use_energy_avg
        self.use_scaled_energy = use_scaled_energy
        self.sampling_rate = sampling_rate
        self.tp = TextProcessing(
            symbol_set,
            cleaner_names,
            heteronyms_path,
            phoneme_dict_path,
            p_phoneme=p_phoneme,
            handle_phoneme=handle_phoneme,
            handle_phoneme_ambiguous=handle_phoneme_ambiguous,
            prepend_space_to_text=prepend_space_to_text,
            append_space_to_text=append_space_to_text,
            add_bos_eos_to_text=add_bos_eos_to_text,
        )

        self.dur_min = dur_min
        self.dur_max = dur_max
        if speaker_ids is None or speaker_ids == "":
            self.speaker_ids = self.create_speaker_lookup_table(self.data)
        else:
            self.speaker_ids = speaker_ids

        print("Number of files", len(self.data))
        if include_speakers is not None:
            for speaker_set, include in include_speakers:
                self.filter_by_speakers_(speaker_set, include)
            print("Number of files after speaker filtering", len(self.data))

        if dur_min is not None and dur_max is not None:
            self.filter_by_duration_(dur_min, dur_max)
            print("Number of files after duration filtering", len(self.data))

        self.use_attn_prior_masking = bool(use_attn_prior_masking)
        self.prepend_space_to_text = bool(prepend_space_to_text)
        self.append_space_to_text = bool(append_space_to_text)
        self.betabinom_cache_path = betabinom_cache_path
        self.betabinom_scaling_factor = betabinom_scaling_factor
        self.lmdb_cache_path = lmdb_cache_path
        if self.lmdb_cache_path != "":
            self.cache_data_lmdb = lmdb.open(
                self.lmdb_cache_path, readonly=True, max_readers=1024, lock=False
            ).begin()

        # make sure caching path exists
        if not os.path.exists(self.betabinom_cache_path):
            os.makedirs(self.betabinom_cache_path)

        print("Dataloader initialized with no augmentations")
        self.speaker_map = None
        if "speaker_map" in kwargs:
            self.speaker_map = kwargs["speaker_map"]

    def load_data(self, datasets, split="|"):
        dataset = []
        for dset_name, dset_dict in datasets.items():
            folder_path = dset_dict["basedir"]
            audiodir = dset_dict["audiodir"]
            filename = dset_dict["filelist"]
            audio_lmdb_key = None
            if "lmdbpath" in dset_dict.keys() and len(dset_dict["lmdbpath"]) > 0:
                self.audio_lmdb_dict[dset_name] = lmdb.open(
                    dset_dict["lmdbpath"], readonly=True, max_readers=256, lock=False
                ).begin()
                audio_lmdb_key = dset_name

            wav_folder_prefix = os.path.join(folder_path, audiodir)
            filelist_path = os.path.join(folder_path, filename)
            with open(filelist_path, encoding="utf-8") as f:
                data = [line.strip().split(split) for line in f]

            for d in data:
                # NOTE (Sam): BEWARE! change/comment depending on filelist.
                duration = -1
                dataset.append(
                    {
                        "audiopath": os.path.join(wav_folder_prefix, d[0]),
                        "text": d[1],
                        "speaker": d[2],  # should be unused
                        "duration": float(duration),
                    }
                )
        return dataset

    def filter_by_speakers_(self, speakers, include=True):
        print("Include spaker {}: {}".format(speakers, include))
        if include:
            self.data = [x for x in self.data if x["speaker"] in speakers]
        else:
            self.data = [x for x in self.data if x["speaker"] not in speakers]

    def filter_by_duration_(self, dur_min, dur_max):
        self.data = [
            x
            for x in self.data
            if x["duration"] == -1
            or (x["duration"] >= dur_min and x["duration"] <= dur_max)
        ]

    def create_speaker_lookup_table(self, data):
        speaker_ids = np.sort(np.unique([x["speaker"] for x in data]))
        d = {speaker_ids[i]: i for i in range(len(speaker_ids))}
        print("Number of speakers:", len(d))
        print("Speaker IDS", d)
        return d

    def f0_normalize(self, x):
        if self.use_log_f0:
            mask = x >= self.f0_min
            x[mask] = torch.log(x[mask])
            x[~mask] = 0.0

        return x

    def f0_denormalize(self, x):
        if self.use_log_f0:
            log_f0_min = np.log(self.f0_min)
            mask = x >= log_f0_min
            x[mask] = torch.exp(x[mask])
            x[~mask] = 0.0
        x[x <= 0.0] = 0.0

        return x

    def energy_avg_normalize(self, x):
        if self.use_scaled_energy:
            x = (x + 20.0) / 20.0
        return x

    def energy_avg_denormalize(self, x):
        if self.use_scaled_energy:
            x = x * 20.0 - 20.0
        return x

    def get_f0_pvoiced(
        self,
        audio,
        sampling_rate=22050,
        frame_length=1024,
        hop_length=256,
        f0_min=100,
        f0_max=300,
    ):
        audio_norm = audio / self.max_wav_value
        f0, voiced_mask, p_voiced = pyin(
            audio_norm,
            f0_min,
            f0_max,
            sampling_rate,
            frame_length=frame_length,
            win_length=frame_length // 2,
            hop_length=hop_length,
        )
        f0[~voiced_mask] = 0.0
        f0 = torch.FloatTensor(f0)
        p_voiced = torch.FloatTensor(p_voiced)
        voiced_mask = torch.FloatTensor(voiced_mask)
        return f0, voiced_mask, p_voiced

    def get_energy_average(self, mel):
        energy_avg = mel.mean(0)
        energy_avg = self.energy_avg_normalize(energy_avg)
        return energy_avg

    def get_mel(self, audio):
        audio_norm = audio / self.max_wav_value
        audio_norm = audio_norm.unsqueeze(0)
        audio_norm = torch.autograd.Variable(audio_norm, requires_grad=False)
        melspec = self.stft.mel_spectrogram(audio_norm)
        melspec = torch.squeeze(melspec, 0)
        if self.do_mel_scaling:
            melspec = (melspec + 5.5) / 2
        if self.mel_noise_scale > 0:
            melspec += torch.randn_like(melspec) * self.mel_noise_scale
        return melspec

    def get_speaker_id(self, speaker):
        if self.speaker_map is not None and speaker in self.speaker_map:
            speaker = self.speaker_map[speaker]

        return torch.LongTensor([self.speaker_ids[speaker]])

    def get_text(self, text):
        text = self.tp.encode_text(text)
        text = torch.LongTensor(text)
        return text

    def get_attention_prior(self, n_tokens, n_frames):
        # cache the entire attn_prior by filename
        if self.use_attn_prior_masking:
            filename = "{}_{}".format(n_tokens, n_frames)
            prior_path = os.path.join(self.betabinom_cache_path, filename)
            prior_path += "_prior.pth"
            if self.lmdb_cache_path != "":
                attn_prior = pkl.loads(
                    self.cache_data_lmdb.get(prior_path.encode("ascii"))
                )
            elif os.path.exists(prior_path):
                attn_prior = torch.load(prior_path)
            else:
                attn_prior = beta_binomial_prior_distribution(
                    n_tokens, n_frames, self.betabinom_scaling_factor
                )
                torch.save(attn_prior, prior_path)
        else:
            attn_prior = torch.ones(n_frames, n_tokens)  # all ones baseline

        return attn_prior

    def __getitem__(self, index):
        data = self.data[index]
        sub_path = data["audiopath"]
        text = data["text"]
        audiopath = f"{sub_path}/resampled_unnormalized.wav"
        audio_emb_path = f"{sub_path}/coqui_resnet_512_emb.pt"
        f0_path = f"{sub_path}/f0.pt"
        mel_path = f"{sub_path}/spectrogram.pt"

        speaker_id = data["speaker"]
        f0, voiced_mask, p_voiced = torch.load(f0_path)
        f0 = self.f0_normalize(f0)
        if self.distance_tx_unvoiced:
            mask = f0 <= 0.0
            distance_map = np.log(distance_transform(mask))
            distance_map[distance_map <= 0] = 0.0
            f0 = f0 - distance_map

        mel = torch.load(mel_path)

        energy_avg = None
        if self.use_energy_avg:
            energy_avg = self.get_energy_average(mel)
            if self.use_scaled_energy and energy_avg.min() < 0.0:
                print(audiopath, "has scaled energy avg smaller than 0")

        speaker_id = self.get_speaker_id(speaker_id)
        text_encoded = self.get_text(text)
        attn_prior = self.get_attention_prior(text_encoded.shape[0], mel.shape[1])

        if not self.use_attn_prior_masking:
            attn_prior = None

        audio_emb = torch.load(audio_emb_path)
        return {
            "mel": mel,
            "speaker_id": speaker_id,
            "text_encoded": text_encoded,
            "audiopath": audiopath,
            "attn_prior": attn_prior,
            "f0": f0,
            "p_voiced": p_voiced,
            "voiced_mask": voiced_mask,
            "energy_avg": energy_avg,
            "audio_embedding": audio_emb,
        }

    def __len__(self):
        return len(self.data)


class DataMel(Dataset):
    def __init__(
        self,
        data_config,
        audiopaths: Optional[List[str]] = None,
    ):
        self.audiopaths = audiopaths
        stft = TacotronSTFT(
            filter_length=data_config["filter_length"],
            hop_length=data_config["hop_length"],
            win_length=data_config["win_length"],
            sampling_rate=22050,
            n_mel_channels=data_config["n_mel_channels"],
            mel_fmin=data_config["mel_fmin"],
            mel_fmax=data_config["mel_fmax"],
        )
        self.stft = stft

    # NOTE (Sam): assumes data is in a directory structure like:
    # /tmp/{uuid}/resampled_unnormalized.wav
    def _get_data(self, audiopath: str):
        rate, audio = read(audiopath)
        sub_path = audiopath.split("resampled_unnormalized.wav")[0]
        audio = np.asarray(audio / (np.abs(audio).max() * 2))
        audio_norm = torch.tensor(audio, dtype=torch.float32)
        audio_norm = audio_norm.unsqueeze(0)
        melspec = self.stft.mel_spectrogram(audio_norm)
        melspec = torch.squeeze(melspec, 0)
        melspec = (melspec + 5.5) / 2
        spec_path_local = f"{sub_path}/spectrogram.pt"
        torch.save(melspec.detach(), spec_path_local)

    def __getitem__(self, idx):
        try:
            self._get_data(audiopath=self.audiopaths[idx])

        except Exception as e:
            print(f"Error while getting data: index = {idx}")
            print(e)
            raise
        return None

    def __len__(self):
        nfiles = len(self.audiopaths)

        return nfiles


# NOTE (Sam): this is the radtts preprocessing.
# TODO (Sam): synthesize with other dataloaders using functional arguments.
from uberduck_ml_dev.models.tacotron2 import MAX_WAV_VALUE
from uberduck_ml_dev.models.components.encoders.resnet_speaker_encoder import (
    get_pretrained_model,
)


class DataEmbedding:
    # NOTE (Sam): subpath_truncation=41 assumes data is in a directory structure like:
    # /tmp/{uuid}/resampled_unnormalized.wav
    def __init__(
        self,
        resnet_se_model_path,
        resnet_se_config_path,
        audiopaths,
        subpath_truncation=41,
    ):
        self.model = get_pretrained_model(
            model_path=resnet_se_model_path, config_path=resnet_se_config_path
        )
        self.audiopaths = audiopaths
        self.subpath_truncation = subpath_truncation

    def _get_data(self, audiopath):
        rate, data = read(audiopath)
        data = torch.FloatTensor(data.astype("float32") / MAX_WAV_VALUE).unsqueeze(0)
        sub_path = audiopath[: self.subpath_truncation]
        embedding = self.model(data).squeeze()
        emb_path_local = f"{sub_path}/coqui_resnet_512_emb.pt"
        torch.save(embedding.detach(), emb_path_local)

    def __getitem__(self, idx):
        try:
            self._get_data(audiopath=self.audiopaths[idx])

        except Exception as e:
            print(f"Error while getting data: index = {idx}")
            print(e)
            raise
        return None

    def __len__(self):
        nfiles = len(self.audiopaths)

        return nfiles


def get_f0_pvoiced(
    audio,
    sampling_rate=22050,
    frame_length=1024,
    hop_length=256,
    f0_min=100,
    f0_max=300,
):
    # NOTE (Sam): is this normalization kosher?
    MAX_WAV_VALUE = 32768.0
    audio_norm = audio / MAX_WAV_VALUE
    f0, voiced_mask, p_voiced = pyin(
        y=audio_norm,
        fmin=f0_min,
        fmax=f0_max,
        sr=sampling_rate,
        frame_length=frame_length,
        win_length=frame_length // 2,
        hop_length=hop_length,
    )
    f0[~voiced_mask] = 0.0
    f0 = torch.FloatTensor(f0)
    p_voiced = torch.FloatTensor(p_voiced)
    voiced_mask = torch.FloatTensor(voiced_mask)
    return f0, voiced_mask, p_voiced


class DataPitch:
    # NOTE (Sam): subpath_truncation=41 assumes data is in a directory structure like:
    # /tmp/{uuid}/resampled_unnormalized.wav
    def __init__(self, data_config, audiopaths, subpath_truncation=41):
        self.hop_length = data_config["hop_length"]
        self.f0_min = data_config["f0_min"]
        self.f0_max = data_config["f0_max"]
        self.frame_length = data_config["filter_length"]
        self.audiopaths = audiopaths
        self.subpath_truncation = subpath_truncation

    def _get_data(self, audiopath):
        rate, data = read(audiopath)
        sub_path = audiopath[: self.subpath_truncation]
        pitch = get_f0_pvoiced(
            data,
            f0_min=self.f0_min,
            f0_max=self.f0_max,
            hop_length=self.hop_length,
            frame_length=self.frame_length,
            sampling_rate=22050,
        )
        pitch_path_local = f"{sub_path}/f0.pt"
        torch.save(pitch, pitch_path_local)

    def __getitem__(self, idx):
        try:
            self._get_data(audiopath=self.audiopaths[idx])

        except Exception as e:
            print(f"Error while getting data: index = {idx}")
            print(e)
            raise
        return None

    def __len__(self):
        nfiles = len(self.audiopaths)

        return nfiles


RADTTS_DEFAULTS = {
    "training_files": {
        "dataset_1": {
            "basedir": "",
            "audiodir": "",
            "filelist": "",
            "lmdbpath": "",
        }
    },
    "validation_files": {
        "dataset_1": {
            "basedir": "",
            "audiodir": "",
            "filelist": "",
            "lmdbpath": "",
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
    "heteronyms_path": "uberduck_ml_dev/text/heteronyms",
    "phoneme_dict_path": "uberduck_ml_dev/text/cmudict-0.7b",
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
    "mel_noise_scale": 0.0,
}
