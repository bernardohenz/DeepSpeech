# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

import os

from functools import partial

import numpy as np
import pandas
import tensorflow as tf
import datetime

from tensorflow.contrib.framework.python.ops import audio_ops as contrib_audio

from util.config import Config
from util.text import text_to_char_array
from util.flags import FLAGS
from util.spectrogram_augmentations import augment_sparse_deform, augment_freq_time_mask, augment_dropout, augment_pitch_and_tempo, augment_speed_up

def read_csvs(csv_files):
    source_data = None
    for csv in csv_files:
        file = pandas.read_csv(csv, encoding='utf-8', na_filter=False)
        #FIXME: not cross-platform
        csv_dir = os.path.dirname(os.path.abspath(csv))
        file['wav_filename'] = file['wav_filename'].str.replace(r'(^[^/])', lambda m: os.path.join(csv_dir, m.group(1))) # pylint: disable=cell-var-from-loop
        if source_data is None:
            source_data = file
        else:
            source_data = source_data.append(file)
    return source_data


def samples_to_mfccs(samples, sample_rate, train_phase=False):
    spectrogram = contrib_audio.audio_spectrogram(samples,
                                                  window_size=Config.audio_window_samples,
                                                  stride=Config.audio_step_samples,
                                                  magnitude_squared=True)

    # Data Augmentations
    if train_phase:
        if FLAGS.augmention_sparse_deform:
            spectrogram = augment_sparse_deform(spectrogram,
                                                time_warping_para=FLAGS.augmentation_time_warp_max_warping,
                                                normal_around_warping_std=FLAGS.augmentation_sparse_deform_std_warp)

        if FLAGS.augmentation_spec_dropout_keeprate < 1:
            spectrogram = augment_dropout(spectrogram,
                                          keep_prob=FLAGS.augmentation_spec_dropout_keeprate)

        if FLAGS.augmentation_freq_and_time_masking:
            spectrogram = augment_freq_time_mask(spectrogram,
                                                 frequency_masking_para=FLAGS.augmentation_freq_and_time_masking_freq_mask_range,
                                                 time_masking_para=FLAGS.augmentation_freq_and_time_masking_time_mask_range,
                                                 frequency_mask_num=FLAGS.augmentation_freq_and_time_masking_number_freq_masks,
                                                 time_mask_num=FLAGS.augmentation_freq_and_time_masking_number_time_masks)

        if FLAGS.augmentation_pitch_and_tempo_scaling:
            spectrogram = augment_pitch_and_tempo(spectrogram,
                                                  max_tempo=FLAGS.augmentation_pitch_and_tempo_scaling_max_tempo,
                                                  max_pitch=FLAGS.augmentation_pitch_and_tempo_scaling_max_pitch,
                                                  min_pitch=FLAGS.augmentation_pitch_and_tempo_scaling_min_pitch)

        if FLAGS.augmentation_speed_up_std > 0:
            spectrogram = augment_speed_up(spectrogram, speed_std=FLAGS.augmentation_speed_up_std)

    mfccs = contrib_audio.mfcc(spectrogram, sample_rate, dct_coefficient_count=Config.n_input)
    mfccs = tf.reshape(mfccs, [-1, Config.n_input])

    return mfccs, tf.shape(mfccs)[0]


def audiofile_to_features(wav_filename, train_phase=False):
    samples = tf.io.read_file(wav_filename)
    decoded = contrib_audio.decode_wav(samples, desired_channels=1)
    features, features_len = samples_to_mfccs(decoded.audio, decoded.sample_rate, train_phase=train_phase)

    if train_phase:
        if FLAGS.data_aug_features_multiplicative > 0:
            features = features*tf.random.normal(mean=1, stddev=FLAGS.data_aug_features_multiplicative, shape=tf.shape(features))

        if FLAGS.data_aug_features_additive > 0:
            features = features+tf.random.normal(mean=0.0, stddev=FLAGS.data_aug_features_additive, shape=tf.shape(features))

    return features, features_len


def entry_to_features_not_augmented(wav_filename, transcript):
    # https://bugs.python.org/issue32117
    features, features_len = audiofile_to_features(wav_filename, train_phase=False)
    return wav_filename, features, features_len, tf.SparseTensor(*transcript)

def entry_to_features_augmented(wav_filename, transcript):
    # https://bugs.python.org/issue32117
    features, features_len = audiofile_to_features(wav_filename, train_phase=True)
    return wav_filename, features, features_len, tf.SparseTensor(*transcript)


def to_sparse_tuple(sequence):
    r"""Creates a sparse representention of ``sequence``.
        Returns a tuple with (indices, values, shape)
    """
    indices = np.asarray(list(zip([0]*len(sequence), range(len(sequence)))), dtype=np.int64)
    shape = np.asarray([1, len(sequence)], dtype=np.int64)
    return indices, sequence, shape


def create_dataset(csvs, batch_size, cache_path='', train_phase=True):
    df = read_csvs(csvs)
    df.sort_values(by='wav_filesize', inplace=True)

    # Convert to character index arrays
    df['transcript'] = df['transcript'].apply(partial(text_to_char_array, alphabet=Config.alphabet))

    entry_to_features = entry_to_features_augmented if train_phase else entry_to_features_not_augmented
    def generate_values():
        for _, row in df.iterrows():
            yield row.wav_filename, to_sparse_tuple(row.transcript)

    # Batching a dataset of 2D SparseTensors creates 3D batches, which fail
    # when passed to tf.nn.ctc_loss, so we reshape them to remove the extra
    # dimension here.
    def sparse_reshape(sparse):
        shape = sparse.dense_shape
        return tf.sparse.reshape(sparse, [shape[0], shape[2]])

    def batch_fn(wav_filenames, features, features_len, transcripts):
        features = tf.data.Dataset.zip((features, features_len))
        features = features.padded_batch(batch_size,
                                         padded_shapes=([None, Config.n_input], []))
        transcripts = transcripts.batch(batch_size).map(sparse_reshape)
        wav_filenames = wav_filenames.batch(batch_size)
        return tf.data.Dataset.zip((wav_filenames, features, transcripts))

    num_gpus = len(Config.available_devices)

    dataset = (tf.data.Dataset.from_generator(generate_values,
                                              output_types=(tf.string, (tf.int64, tf.int32, tf.int64)))
                              .map(entry_to_features, num_parallel_calls=tf.data.experimental.AUTOTUNE)
                              .cache(cache_path)
                              .window(batch_size, drop_remainder=True).flat_map(batch_fn)
                              .prefetch(num_gpus))

    return dataset

def secs_to_hours(secs):
    hours, remainder = divmod(secs, 3600)
    minutes, seconds = divmod(remainder, 60)
    return '%d:%02d:%02d' % (hours, minutes, seconds)
