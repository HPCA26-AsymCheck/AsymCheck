# coding=utf-8
# Copyright (c) 2019-2021 NVIDIA CORPORATION. All rights reserved.
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Run BERT on SQuAD."""

from __future__ import absolute_import, division, print_function

import argparse
import collections
import json
import logging
import math
import os
import random
import sys
from io import open
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange


from apex import amp
from schedulers import LinearWarmUpScheduler
from file_utils import PYTORCH_PRETRAINED_BERT_CACHE
import modeling
from optimization import BertAdam, warmup_linear
from tokenization import (BasicTokenizer, BertTokenizer, whitespace_tokenize)
from utils import is_main_process, format_step
import dllogger, time


torch._C._jit_set_profiling_mode(False)
torch._C._jit_set_profiling_executor(False)

if sys.version_info[0] == 2:
    import cPickle as pickle
else:
    import pickle

import argparse
import collections
import logging
import json
import math
import os
import random
import pickle
from tqdm import tqdm, trange
from utils_bert_ds_no_initckpt import get_argument_parser, \
    get_summary_writer, write_summary_events, \
    is_time_to_exit, check_early_exit_warning
import deepspeed

import time
import numpy as np
import torch
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler


import shutil
import uuid
import torchsnapshot
from typing import Dict, Optional
from torchsnapshot import Snapshot, Stateful
import torch.distributed as dist
from tqdm import tqdm
from multiprocessing import shared_memory
import threading



logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.WARNING)

logger = logging.getLogger(__name__)



import argparse
import time
import math
import os
import torch
import torch.onnx

import torch
import argparse
import torch.backends.cudnn as cudnn
import torch.multiprocessing as mp
import torch.nn.functional as F
import torch.optim as optim
import torch.utils.data.distributed
import os
import math

import time
import os


import numpy as np
import matplotlib.pyplot as plt
import time
import timeit

# snapshot
import uuid
import shutil
from typing import Dict, Optional
import torchsnapshot
from torchsnapshot import Snapshot, Stateful


try:
    mp.set_start_method('spawn')
except RuntimeError:
    pass



class SquadExample(object):
    """
    A single training/test example for the Squad dataset.
    For examples without an answer, the start and end position are -1.
    """

    def __init__(self,
                 qas_id,
                 question_text,
                 doc_tokens,
                 orig_answer_text=None,
                 start_position=None,
                 end_position=None,
                 is_impossible=None):
        self.qas_id = qas_id
        self.question_text = question_text
        self.doc_tokens = doc_tokens
        self.orig_answer_text = orig_answer_text
        self.start_position = start_position
        self.end_position = end_position
        self.is_impossible = is_impossible

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        s = ""
        s += "qas_id: %s" % (self.qas_id)
        s += ", question_text: %s" % (
            self.question_text)
        s += ", doc_tokens: [%s]" % (" ".join(self.doc_tokens))
        if self.start_position:
            s += ", start_position: %d" % (self.start_position)
        if self.end_position:
            s += ", end_position: %d" % (self.end_position)
        if self.is_impossible:
            s += ", is_impossible: %r" % (self.is_impossible)
        return s


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self,
                 unique_id,
                 example_index,
                 doc_span_index,
                 tokens,
                 token_to_orig_map,
                 token_is_max_context,
                 input_ids,
                 input_mask,
                 segment_ids,
                 start_position=None,
                 end_position=None,
                 is_impossible=None):
        self.unique_id = unique_id
        self.example_index = example_index
        self.doc_span_index = doc_span_index
        self.tokens = tokens
        self.token_to_orig_map = token_to_orig_map
        self.token_is_max_context = token_is_max_context
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.start_position = start_position
        self.end_position = end_position
        self.is_impossible = is_impossible


def read_squad_examples(input_file, is_training, version_2_with_negative):
    """Read a SQuAD json file into a list of SquadExample."""
    with open(input_file, "r", encoding='utf-8') as reader:
        input_data = json.load(reader)["data"]

    def is_whitespace(c):
        if c == " " or c == "\t" or c == "\r" or c == "\n" or ord(c) == 0x202F:
            return True
        return False

    examples = []
    for entry in input_data:
        for paragraph in entry["paragraphs"]:
            paragraph_text = paragraph["context"]
            doc_tokens = []
            char_to_word_offset = []
            prev_is_whitespace = True
            for c in paragraph_text:
                if is_whitespace(c):
                    prev_is_whitespace = True
                else:
                    if prev_is_whitespace:
                        doc_tokens.append(c)
                    else:
                        doc_tokens[-1] += c
                    prev_is_whitespace = False
                char_to_word_offset.append(len(doc_tokens) - 1)

            for qa in paragraph["qas"]:
                qas_id = qa["id"]
                question_text = qa["question"]
                start_position = None
                end_position = None
                orig_answer_text = None
                is_impossible = False
                if is_training:
                    if version_2_with_negative:
                        is_impossible = qa["is_impossible"]
                    if (len(qa["answers"]) != 1) and (not is_impossible):
                        raise ValueError(
                            "For training, each question should have exactly 1 answer.")
                    if not is_impossible:
                        answer = qa["answers"][0]
                        orig_answer_text = answer["text"]
                        answer_offset = answer["answer_start"]
                        answer_length = len(orig_answer_text)
                        start_position = char_to_word_offset[answer_offset]
                        end_position = char_to_word_offset[answer_offset + answer_length - 1]
                        # Only add answers where the text can be exactly recovered from the
                        # document. If this CAN'T happen it's likely due to weird Unicode
                        # stuff so we will just skip the example.
                        #
                        # Note that this means for training mode, every example is NOT
                        # guaranteed to be preserved.
                        actual_text = " ".join(doc_tokens[start_position:(end_position + 1)])
                        cleaned_answer_text = " ".join(
                            whitespace_tokenize(orig_answer_text))
                        if actual_text.find(cleaned_answer_text) == -1:
                            logger.warning("Could not find answer: '%s' vs. '%s'",
                                           actual_text, cleaned_answer_text)
                            continue
                    else:
                        start_position = -1
                        end_position = -1
                        orig_answer_text = ""

                example = SquadExample(
                    qas_id=qas_id,
                    question_text=question_text,
                    doc_tokens=doc_tokens,
                    orig_answer_text=orig_answer_text,
                    start_position=start_position,
                    end_position=end_position,
                    is_impossible=is_impossible)
                examples.append(example)
    return examples


def convert_examples_to_features(examples, tokenizer, max_seq_length,
                                 doc_stride, max_query_length, is_training):
    """Loads a data file into a list of `InputBatch`s."""

    unique_id = 1000000000

    features = []
    for (example_index, example) in enumerate(examples):
        query_tokens = tokenizer.tokenize(example.question_text)

        if len(query_tokens) > max_query_length:
            query_tokens = query_tokens[0:max_query_length]

        tok_to_orig_index = []
        orig_to_tok_index = []
        all_doc_tokens = []
        for (i, token) in enumerate(example.doc_tokens):
            orig_to_tok_index.append(len(all_doc_tokens))
            sub_tokens = tokenizer.tokenize(token)
            for sub_token in sub_tokens:
                tok_to_orig_index.append(i)
                all_doc_tokens.append(sub_token)

        tok_start_position = None
        tok_end_position = None
        if is_training and example.is_impossible:
            tok_start_position = -1
            tok_end_position = -1
        if is_training and not example.is_impossible:
            tok_start_position = orig_to_tok_index[example.start_position]
            if example.end_position < len(example.doc_tokens) - 1:
                tok_end_position = orig_to_tok_index[example.end_position + 1] - 1
            else:
                tok_end_position = len(all_doc_tokens) - 1
            (tok_start_position, tok_end_position) = _improve_answer_span(
                all_doc_tokens, tok_start_position, tok_end_position, tokenizer,
                example.orig_answer_text)

        # The -3 accounts for [CLS], [SEP] and [SEP]
        max_tokens_for_doc = max_seq_length - len(query_tokens) - 3

        # We can have documents that are longer than the maximum sequence length.
        # To deal with this we do a sliding window approach, where we take chunks
        # of the up to our max length with a stride of `doc_stride`.
        _DocSpan = collections.namedtuple(  # pylint: disable=invalid-name
            "DocSpan", ["start", "length"])
        doc_spans = []
        start_offset = 0
        while start_offset < len(all_doc_tokens):
            length = len(all_doc_tokens) - start_offset
            if length > max_tokens_for_doc:
                length = max_tokens_for_doc
            doc_spans.append(_DocSpan(start=start_offset, length=length))
            if start_offset + length == len(all_doc_tokens):
                break
            start_offset += min(length, doc_stride)

        for (doc_span_index, doc_span) in enumerate(doc_spans):
            tokens = []
            token_to_orig_map = {}
            token_is_max_context = {}
            segment_ids = []
            tokens.append("[CLS]")
            segment_ids.append(0)
            for token in query_tokens:
                tokens.append(token)
                segment_ids.append(0)
            tokens.append("[SEP]")
            segment_ids.append(0)

            for i in range(doc_span.length):
                split_token_index = doc_span.start + i
                token_to_orig_map[len(tokens)] = tok_to_orig_index[split_token_index]

                is_max_context = _check_is_max_context(doc_spans, doc_span_index,
                                                       split_token_index)
                token_is_max_context[len(tokens)] = is_max_context
                tokens.append(all_doc_tokens[split_token_index])
                segment_ids.append(1)
            tokens.append("[SEP]")
            segment_ids.append(1)

            input_ids = tokenizer.convert_tokens_to_ids(tokens)

            # The mask has 1 for real tokens and 0 for padding tokens. Only real
            # tokens are attended to.
            input_mask = [1] * len(input_ids)

            # Zero-pad up to the sequence length.
            while len(input_ids) < max_seq_length:
                input_ids.append(0)
                input_mask.append(0)
                segment_ids.append(0)

            assert len(input_ids) == max_seq_length
            assert len(input_mask) == max_seq_length
            assert len(segment_ids) == max_seq_length

            start_position = None
            end_position = None
            if is_training and not example.is_impossible:
                # For training, if our document chunk does not contain an annotation
                # we throw it out, since there is nothing to predict.
                doc_start = doc_span.start
                doc_end = doc_span.start + doc_span.length - 1
                out_of_span = False
                if not (tok_start_position >= doc_start and
                        tok_end_position <= doc_end):
                    out_of_span = True
                if out_of_span:
                    start_position = 0
                    end_position = 0
                else:
                    doc_offset = len(query_tokens) + 2
                    start_position = tok_start_position - doc_start + doc_offset
                    end_position = tok_end_position - doc_start + doc_offset
            if is_training and example.is_impossible:
                start_position = 0
                end_position = 0

            features.append(
                InputFeatures(
                    unique_id=unique_id,
                    example_index=example_index,
                    doc_span_index=doc_span_index,
                    tokens=tokens,
                    token_to_orig_map=token_to_orig_map,
                    token_is_max_context=token_is_max_context,
                    input_ids=input_ids,
                    input_mask=input_mask,
                    segment_ids=segment_ids,
                    start_position=start_position,
                    end_position=end_position,
                    is_impossible=example.is_impossible))
            unique_id += 1

    return features


def _improve_answer_span(doc_tokens, input_start, input_end, tokenizer,
                         orig_answer_text):
    """Returns tokenized answer spans that better match the annotated answer."""

    # The SQuAD annotations are character based. We first project them to
    # whitespace-tokenized words. But then after WordPiece tokenization, we can
    # often find a "better match". For example:
    #
    #   Question: What year was John Smith born?
    #   Context: The leader was John Smith (1895-1943).
    #   Answer: 1895
    #
    # The original whitespace-tokenized answer will be "(1895-1943).". However
    # after tokenization, our tokens will be "( 1895 - 1943 ) .". So we can match
    # the exact answer, 1895.
    #
    # However, this is not always possible. Consider the following:
    #
    #   Question: What country is the top exporter of electornics?
    #   Context: The Japanese electronics industry is the lagest in the world.
    #   Answer: Japan
    #
    # In this case, the annotator chose "Japan" as a character sub-span of
    # the word "Japanese". Since our WordPiece tokenizer does not split
    # "Japanese", we just use "Japanese" as the annotation. This is fairly rare
    # in SQuAD, but does happen.
    tok_answer_text = " ".join(tokenizer.tokenize(orig_answer_text))

    for new_start in range(input_start, input_end + 1):
        for new_end in range(input_end, new_start - 1, -1):
            text_span = " ".join(doc_tokens[new_start:(new_end + 1)])
            if text_span == tok_answer_text:
                return (new_start, new_end)

    return (input_start, input_end)


def _check_is_max_context(doc_spans, cur_span_index, position):
    """Check if this is the 'max context' doc span for the token."""

    # Because of the sliding window approach taken to scoring documents, a single
    # token can appear in multiple documents. E.g.
    #  Doc: the man went to the store and bought a gallon of milk
    #  Span A: the man went to the
    #  Span B: to the store and bought
    #  Span C: and bought a gallon of
    #  ...
    #
    # Now the word 'bought' will have two scores from spans B and C. We only
    # want to consider the score with "maximum context", which we define as
    # the *minimum* of its left and right context (the *sum* of left and
    # right context will always be the same, of course).
    #
    # In the example the maximum context for 'bought' would be span C since
    # it has 1 left context and 3 right context, while span B has 4 left context
    # and 0 right context.
    best_score = None
    best_span_index = None
    for (span_index, doc_span) in enumerate(doc_spans):
        end = doc_span.start + doc_span.length - 1
        if position < doc_span.start:
            continue
        if position > end:
            continue
        num_left_context = position - doc_span.start
        num_right_context = end - position
        score = min(num_left_context, num_right_context) + 0.01 * doc_span.length
        if best_score is None or score > best_score:
            best_score = score
            best_span_index = span_index

    return cur_span_index == best_span_index


RawResult = collections.namedtuple("RawResult",
                                   ["unique_id", "start_logits", "end_logits"])


def get_answers(examples, features, results, args):
    predictions = collections.defaultdict(list) #it is possible that one example corresponds to multiple features
    Prediction = collections.namedtuple('Prediction', ['text', 'start_logit', 'end_logit'])

    if args.version_2_with_negative:
        null_vals = collections.defaultdict(lambda: (float("inf"),0,0))
    for ex, feat, result in match_results(examples, features, results):
        start_indices = _get_best_indices(result.start_logits, args.n_best_size)
        end_indices = _get_best_indices(result.end_logits, args.n_best_size)
        prelim_predictions = get_valid_prelim_predictions(start_indices, end_indices, feat, result, args)
        prelim_predictions = sorted(
                            prelim_predictions,
                            key=lambda x: (x.start_logit + x.end_logit),
                            reverse=True)
        if args.version_2_with_negative:
            score = result.start_logits[0] + result.end_logits[0]
            if score < null_vals[ex.qas_id][0]:
                null_vals[ex.qas_id] = (score, result.start_logits[0], result.end_logits[0])

        curr_predictions = []
        seen_predictions = []
        for pred in prelim_predictions:
            if len(curr_predictions) == args.n_best_size:
                break
            if pred.start_index > 0:  # this is a non-null prediction TODO: this probably is irrelevant
                final_text = get_answer_text(ex, feat, pred, args)
                if final_text in seen_predictions:
                    continue
            else:
                final_text = ""

            seen_predictions.append(final_text)
            curr_predictions.append(Prediction(final_text, pred.start_logit, pred.end_logit))
        predictions[ex.qas_id] += curr_predictions

    #Add empty prediction
    if args.version_2_with_negative:
        for qas_id in predictions.keys():
            predictions[qas_id].append(Prediction('',
                                                  null_vals[ex.qas_id][1],
                                                  null_vals[ex.qas_id][2]))


    nbest_answers = collections.defaultdict(list)
    answers = {}
    for qas_id, preds in predictions.items():
        nbest = sorted(
                preds,
                key=lambda x: (x.start_logit + x.end_logit),
                reverse=True)[:args.n_best_size]

        # In very rare edge cases we could only have single null prediction.
        # So we just create a nonce prediction in this case to avoid failure.
        if not nbest:
            nbest.append(Prediction(text="empty", start_logit=0.0, end_logit=0.0))

        total_scores = []
        best_non_null_entry = None
        for entry in nbest:
            total_scores.append(entry.start_logit + entry.end_logit)
            if not best_non_null_entry and entry.text:
                best_non_null_entry = entry
        probs = _compute_softmax(total_scores)
        for (i, entry) in enumerate(nbest):
            output = collections.OrderedDict()
            output["text"] = entry.text
            output["probability"] = probs[i]
            output["start_logit"] = entry.start_logit
            output["end_logit"] = entry.end_logit
            nbest_answers[qas_id].append(output)
        if args.version_2_with_negative:
            score_diff = null_vals[qas_id][0] - best_non_null_entry.start_logit - best_non_null_entry.end_logit
            if score_diff > args.null_score_diff_threshold:
                answers[qas_id] = ""
            else:
                answers[qas_id] = best_non_null_entry.text
        else:
            answers[qas_id] = nbest_answers[qas_id][0]['text']

    return answers, nbest_answers

def get_answer_text(example, feature, pred, args):
    tok_tokens = feature.tokens[pred.start_index:(pred.end_index + 1)]
    orig_doc_start = feature.token_to_orig_map[pred.start_index]
    orig_doc_end = feature.token_to_orig_map[pred.end_index]
    orig_tokens = example.doc_tokens[orig_doc_start:(orig_doc_end + 1)]
    tok_text = " ".join(tok_tokens)

    # De-tokenize WordPieces that have been split off.
    tok_text = tok_text.replace(" ##", "")
    tok_text = tok_text.replace("##", "")

    # Clean whitespace
    tok_text = tok_text.strip()
    tok_text = " ".join(tok_text.split())
    orig_text = " ".join(orig_tokens)

    final_text = get_final_text(tok_text, orig_text, args.do_lower_case, args.verbose_logging)
    return final_text

def get_valid_prelim_predictions(start_indices, end_indices, feature, result, args):

    _PrelimPrediction = collections.namedtuple(
        "PrelimPrediction",
        ["start_index", "end_index", "start_logit", "end_logit"])
    prelim_predictions = []
    for start_index in start_indices:
        for end_index in end_indices:
            if start_index >= len(feature.tokens):
                continue
            if end_index >= len(feature.tokens):
                continue
            if start_index not in feature.token_to_orig_map:
                continue
            if end_index not in feature.token_to_orig_map:
                continue
            if not feature.token_is_max_context.get(start_index, False):
                continue
            if end_index < start_index:
                continue
            length = end_index - start_index + 1
            if length > args.max_answer_length:
                continue
            prelim_predictions.append(
                _PrelimPrediction(
                    start_index=start_index,
                    end_index=end_index,
                    start_logit=result.start_logits[start_index],
                    end_logit=result.end_logits[end_index]))
    return prelim_predictions

def match_results(examples, features, results):
    unique_f_ids = set([f.unique_id for f in features])
    unique_r_ids = set([r.unique_id for r in results])
    matching_ids = unique_f_ids & unique_r_ids
    features = [f for f in features if f.unique_id in matching_ids]
    results = [r for r in results if r.unique_id in matching_ids]
    features.sort(key=lambda x: x.unique_id)
    results.sort(key=lambda x: x.unique_id)

    for f, r in zip(features, results): #original code assumes strict ordering of examples. TODO: rewrite this
        yield examples[f.example_index], f, r

def get_final_text(pred_text, orig_text, do_lower_case, verbose_logging=False):
    """Project the tokenized prediction back to the original text."""

    # When we created the data, we kept track of the alignment between original
    # (whitespace tokenized) tokens and our WordPiece tokenized tokens. So
    # now `orig_text` contains the span of our original text corresponding to the
    # span that we predicted.
    #
    # However, `orig_text` may contain extra characters that we don't want in
    # our prediction.
    #
    # For example, let's say:
    #   pred_text = steve smith
    #   orig_text = Steve Smith's
    #
    # We don't want to return `orig_text` because it contains the extra "'s".
    #
    # We don't want to return `pred_text` because it's already been normalized
    # (the SQuAD eval script also does punctuation stripping/lower casing but
    # our tokenizer does additional normalization like stripping accent
    # characters).
    #
    # What we really want to return is "Steve Smith".
    #
    # Therefore, we have to apply a semi-complicated alignment heruistic between
    # `pred_text` and `orig_text` to get a character-to-charcter alignment. This
    # can fail in certain cases in which case we just return `orig_text`.

    def _strip_spaces(text):
        ns_chars = []
        ns_to_s_map = collections.OrderedDict()
        for (i, c) in enumerate(text):
            if c == " ":
                continue
            ns_to_s_map[len(ns_chars)] = i
            ns_chars.append(c)
        ns_text = "".join(ns_chars)
        return (ns_text, ns_to_s_map)

    # We first tokenize `orig_text`, strip whitespace from the result
    # and `pred_text`, and check if they are the same length. If they are
    # NOT the same length, the heuristic has failed. If they are the same
    # length, we assume the characters are one-to-one aligned.

    tokenizer = BasicTokenizer(do_lower_case=do_lower_case)

    tok_text = " ".join(tokenizer.tokenize(orig_text))

    start_position = tok_text.find(pred_text)
    if start_position == -1:
        if verbose_logging:
            logger.info(
                "Unable to find text: '%s' in '%s'" % (pred_text, orig_text))
        return orig_text
    end_position = start_position + len(pred_text) - 1

    (orig_ns_text, orig_ns_to_s_map) = _strip_spaces(orig_text)
    (tok_ns_text, tok_ns_to_s_map) = _strip_spaces(tok_text)

    if len(orig_ns_text) != len(tok_ns_text):
        if verbose_logging:
            logger.info("Length not equal after stripping spaces: '%s' vs '%s'",
                        orig_ns_text, tok_ns_text)
        return orig_text

    # We then project the characters in `pred_text` back to `orig_text` using
    # the character-to-character alignment.
    tok_s_to_ns_map = {}
    for (i, tok_index) in tok_ns_to_s_map.items():
        tok_s_to_ns_map[tok_index] = i

    orig_start_position = None
    if start_position in tok_s_to_ns_map:
        ns_start_position = tok_s_to_ns_map[start_position]
        if ns_start_position in orig_ns_to_s_map:
            orig_start_position = orig_ns_to_s_map[ns_start_position]

    if orig_start_position is None:
        if verbose_logging:
            logger.info("Couldn't map start position")
        return orig_text

    orig_end_position = None
    if end_position in tok_s_to_ns_map:
        ns_end_position = tok_s_to_ns_map[end_position]
        if ns_end_position in orig_ns_to_s_map:
            orig_end_position = orig_ns_to_s_map[ns_end_position]

    if orig_end_position is None:
        if verbose_logging:
            logger.info("Couldn't map end position")
        return orig_text

    output_text = orig_text[orig_start_position:(orig_end_position + 1)]
    return output_text


def _get_best_indices(logits, n_best_size):
    """Get the n-best logits from a list."""
    index_and_score = sorted(enumerate(logits), key=lambda x: x[1], reverse=True)

    best_indices = []
    for i in range(len(index_and_score)):
        if i >= n_best_size:
            break
        best_indices.append(index_and_score[i][0])
    return best_indices


def _compute_softmax(scores):
    """Compute softmax probability over raw logits."""
    if not scores:
        return []

    max_score = None
    for score in scores:
        if max_score is None or score > max_score:
            max_score = score

    exp_scores = []
    total_sum = 0.0
    for score in scores:
        x = math.exp(score - max_score)
        exp_scores.append(x)
        total_sum += x

    probs = []
    for score in exp_scores:
        probs.append(score / total_sum)
    return probs



from apex.multi_tensor_apply import multi_tensor_applier
class GradientClipper:
    """
    Clips gradient norm of an iterable of parameters.
    """
    def __init__(self, max_grad_norm):
        self.max_norm = max_grad_norm
        # if multi_tensor_applier.available:
        # # if False:
        #     import amp_C
        #     self._overflow_buf = amp_C.IntTensor([0])
        #     self.multi_tensor_l2norm = amp_C.multi_tensor_l2norm
        #     self.multi_tensor_scale = amp_C.multi_tensor_scale
        # else:
        #     raise RuntimeError('Gradient clipping requires cuda extensions')

    # def step(self, parameters):
    #     l = [p.grad for p in parameters if p.grad is not None]
    #     total_norm, _ = multi_tensor_applier(self.multi_tensor_l2norm, self._overflow_buf, [l], False)
    #     total_norm = total_norm.item()
    #     if (total_norm == float('inf')): return
    #     clip_coef = self.max_norm / (total_norm + 1e-6)
    #     if clip_coef < 1:
    #         multi_tensor_applier(self.multi_tensor_scale, self._overflow_buf, [l, l], clip_coef)


def full_train():
    global_step = 0
    model.train()
    # gradClipper = GradientClipper(max_grad_norm=1.0)
    train_start = time.time()
    
    # gpu_tensor = torch.randn(100000, 50000, device=device)
    
    for epoch in range(0, int(args.num_train_epochs) + 1):

        io_time_array = []
        forward_backforward_time_array = []
        forward_time_array  = []
        backward_time_array = [] 
        
        communication_time_array  = []
        step_time_array    = []
        elastic_time_array = []
        batch_time_array = []
        
        optimizer.synchronize_time= []
        # optimizer.handle_synchronize_time= []    
        # optimizer_synchronize_time_array= []
        
        model.train()

        train_sampler.set_epoch(epoch)            

        train_iter = tqdm(train_dataloader, desc="Iteration", disable=args.disable_progress_bar) if is_main_process() else train_dataloader

        model.backward_time_array  = []
        model.allreduce_time_array = []
        
        step = 10
        forworad_time_array = []
        backworad_time_array = []
        step_time_array = []
        batch_time_array = []

        ckpt_time_array = []

        active_dataloader = train_dataloader
        s_time = time.time()
        
        
        
        for idx, batch_data in enumerate(train_iter):             
            forworad_time = time.time()
            
            process_optimizer = threading.Thread(target=first_second_copy_optimizer_async, args=(optimizer,))
            process_optimizer.start()
            if args.max_steps > 0 and global_step > args.max_steps:
                break

            batch_data = tuple(t.to(device) for t in batch_data) 
            
            
            input_ids, input_mask, segment_ids, start_positions, end_positions = batch_data
            start_logits, end_logits = model(input_ids, segment_ids, input_mask)

            # If we are on multi-GPU, split add a dimension
            if len(start_positions.size()) > 1:
                start_positions = start_positions.squeeze(-1)
            if len(end_positions.size()) > 1:
                end_positions = end_positions.squeeze(-1)
                
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            start_positions.clamp_(0, ignored_index)
            end_positions.clamp_(0, ignored_index)

            loss_fct = torch.nn.CrossEntropyLoss(ignore_index=ignored_index)
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            loss = (start_loss + end_loss) / 2
            final_loss = loss.item()
            
            # forworad_time_array.append(time.time() - forworad_time)
            
                
            if n_gpu > 1:
                loss = loss.mean()  # mean() to average on multi-gpu.
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps
            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                # loss.backward()
                forworad_time_array.append(time.time() - forworad_time)
                
                
                backworad_time = time.time()
                model.backward(loss)
                
                backworad_time_array.append(time.time() - backworad_time)
            
            if idx == 40 and dist.get_rank() % 2 == 0 :
                optimizer.process_model.join()
                process_optimizer.join()
                optimizer.save_ckpt_to_disk_sync(optimizer.model_data, cpu_optimizer_array_avg, cpu_optimizer_array_avg_sq, dist.get_rank())
                # optimizer.start_queue.put((0))
                # optimizer.module_queue.put((optimizer.model_data, optimizer.model_data))
                # optimizer.optimizer_queue.put((cpu_optimizer_array_avg, cpu_optimizer_array_avg, cpu_optimizer_array_avg_sq, cpu_optimizer_array_avg_sq))
            
            step_time = time.time()
            
            # gradient clipping
            if (idx + 1) % args.gradient_accumulation_steps == 0:
                if args.fp16 :
                    # modify learning rate with special warm up for BERT which FusedAdam doesn't do
                    scheduler.step()
                # optimizer.step()
                model.step()

                optimizer.zero_grad()
                global_step += 1
            
            step_time_array.append(time.time() - step_time)
            
            cpu_optimizer_array_avg.clear()
            cpu_optimizer_array_avg_sq.clear()
            optimizer.model_data.clear()
            optimizer.optimizer_avg_data.clear()
            optimizer.optimizer_avg_sq_data.clear()
            
            if idx % step == 0 and dist.get_rank()==0:
            
                dllogger.log(step=(epoch, global_step,), data={"step_loss": final_loss,
                                "learning_rate": optimizer.param_groups[0]['lr']})
                print('Average Forward Time = ', sum(forworad_time_array)/10)
                print('Average Backward Time = ', sum(backworad_time_array)/10)
                print('Average Step Time = ', sum(step_time_array)/10)
                    
                print('Average Ckpt Time = ', sum(ckpt_time_array)/10)
                print('Average Iteration Time = ', (time.time() -s_time)/10)

                # 
 

                s_time = time.time()

                forworad_time_array = []
                backworad_time_array = []
                step_time_array = []
                ckpt_time_array = []

        train_sampler.set_epoch(epoch)


    time_to_train = time.time() - train_start




def delete_folder_contents(folder):
    for filename in os.listdir(folder):
        file_path = os.path.join(folder, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            print(f'Failed to delete {file_path}. Reason: {e}')



def flush_to_disk(idx, freq, ranks_per_node):
    if idx == freq and dist.get_rank() % ranks_per_node == 0 :
        optimizer.process_model.join()
        process_optimizer.join()
        optimizer.save_ckpt_to_disk_sync(optimizer.model_data_flush, optimizer.optimizer_avg_data, optimizer.optimizer_avg_sq_data, dist.get_rank())
        # 
        # optimizer.start_queue.put((0))
        # optimizer.module_queue.put((optimizer.model_data, optimizer.model_data))
        # optimizer.optimizer_queue.put((cpu_optimizer_array_avg, cpu_optimizer_array_avg, cpu_optimizer_array_avg_sq, cpu_optimizer_array_avg_sq))

    return



# 
def calculate_in_memory_ckpt_time(model , optimizer,  idx):

    in_memory_time = time.time()
    _model_state_dict_cpu = {}
    numel_count = 0
    
    
    for key, value in model.state_dict().items():
        t_cpu = torch.zeros(value.numel(), device='cpu', dtype=value.dtype, requires_grad=False)
        _model_state_dict_cpu[key] = t_cpu                    
        
        value_clone = value.clone()
        
        _model_state_dict_cpu[key].copy_(value_clone.view(value.numel()), non_blocking=True)
        
        numel_count += value.numel()
        

    
    print('model_state_in_memory_time = ', time.time()- in_memory_time)


    in_memory_time = time.time()
    
    if optimizer.state_dict()['optimizer_state_dict']['state']!={} and True:
        exp_avg_0_numel = optimizer.state_dict()['optimizer_state_dict']['state'][0]['exp_avg'].numel()
        exp_avg_sq_0_numel = optimizer.state_dict()['optimizer_state_dict']['state'][0]['exp_avg_sq'].numel()

        exp_avg = optimizer.state_dict()['optimizer_state_dict']['state'][0]['exp_avg']
        exp_avg_cpu = torch.zeros(exp_avg_0_numel, device='cpu', dtype=exp_avg.dtype, requires_grad=False)
        exp_avg_cpu.copy_(exp_avg.view(exp_avg_0_numel), non_blocking=True)
        
        
        exp_avg_sq = optimizer.state_dict()['optimizer_state_dict']['state'][0]['exp_avg']
        exp_avg_sq_cpu = torch.zeros(exp_avg_sq_0_numel, device='cpu', dtype=exp_avg_sq.dtype, requires_grad=False)
        exp_avg_sq_cpu.copy_(exp_avg_sq.view(exp_avg_sq_0_numel), non_blocking=True)


        fp32_flat_groups_0 = optimizer.state_dict()['fp32_flat_groups'][0]
        fp32_flat_groups_0_numel =fp32_flat_groups_0.numel()
        fp32_flat_groups_0_cpu = torch.zeros(fp32_flat_groups_0_numel, device='cpu', dtype=fp32_flat_groups_0.dtype, requires_grad=False)
        fp32_flat_groups_0_cpu.copy_(exp_avg_sq_cpu.view(fp32_flat_groups_0_numel), non_blocking=True) 
        
    print('optimizer_state_in_memory_time = ', time.time()- in_memory_time)
    
    return



def save_checkpoint_bert():
     
    if args.do_train and is_main_process() and not args.skip_checkpoint:
        # Save a trained model and the associated configuration
        model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
        output_model_file = os.path.join(args.output_dir, modeling.WEIGHTS_NAME)
        
        torch.save({"model":model_to_save.state_dict()}, output_model_file)
        
        output_config_file = os.path.join(args.output_dir, modeling.CONFIG_NAME)
        with open(output_config_file, 'w') as f:
            f.write(model_to_save.config.to_json_string())
    
    return


def save_checkpoint(epoch):
    if dist.get_rank() == 0:
        filepath = args.checkpoint_format.format(epoch=epoch + 1)
        state = {
            'model': model.state_dict(),
            'optimizer': optimizer.state_dict(),
            }
        torch.save(state, filepath)


def save_checkpoint_in_disk(epoch):
    if dist.get_rank() == 0:
        filepath = args.checkpoint_format.format(epoch=epoch + 1)
                
        
        state = {
            'model': model.state_dict(),
            'optimizer': optimizer.state_dict(),
        }
        torch.save(state, filepath)



def save_checkpoint_in_memory(epoch):
    if dist.get_rank() == 0:
        # _state_dict_cpu = {}
        
        

        for key, value in model.state_dict().items():
            
            _state_dict_cpu[key].copy_(value.view(value.numel()), non_blocking=True)

    return



def save_checkpoint_in_disk_snapshot(checkpoint_save_work_dir):
    
    if 0==0:
        # delete_folder_contents(checkpoint_save_work_dir)

        print('delete_folder_contents')
        
        # torchsnapshot: take snapshot
        progress["current_epoch"] += 1
        snapshot = torchsnapshot.Snapshot.take(
            # f"{checkpoint_save_work_dir}/run-{uuid.uuid4()}-epoch-{progress['current_epoch']}-model",
            # f"{checkpoint_save_work_dir}/run-{uuid.uuid4()}-epoch-{progress['current_epoch']}-optimizer",
            f"{checkpoint_save_work_dir}/run-{uuid.uuid4()}-epoch-{progress['current_epoch']}-model-optimizer",
            app_state,
            replicated=["**"],
            # this pattern treats all states as replicated
        )
        
    return



def save_checkpoint_in_disk_snapshot_para(app_state, checkpoint_save_work_dir):
    
    if dist.get_rank()==0:
        delete_folder_contents(checkpoint_save_work_dir)
        
        # torchsnapshot: take snapshot
        progress["current_epoch"] += 1
        snapshot = torchsnapshot.Snapshot.take(
            # f"{checkpoint_save_work_dir}/run-{uuid.uuid4()}-epoch-{progress['current_epoch']}-model",
            # f"{checkpoint_save_work_dir}/run-{uuid.uuid4()}-epoch-{progress['current_epoch']}-optimizer",
            f"{checkpoint_save_work_dir}/run-{uuid.uuid4()}-epoch-{progress['current_epoch']}-model-optimizer",
            app_state,
            replicated=["**"],
            # this pattern treats all states as replicated
        )
    return



def end_epoch(state):
    state.epoch += 1
    state.batch = 0
    state.train_sampler.set_epoch(state.epoch)
    state.commit()


def evaluation():
    
    if args.do_predict and (args.local_rank == -1 or is_main_process()):

        if not args.do_train and args.fp16:
            model.half()

        eval_examples = read_squad_examples(
            input_file=args.predict_file, is_training=False, version_2_with_negative=args.version_2_with_negative)
        
        eval_features = convert_examples_to_features(
            examples=eval_examples,
            tokenizer=tokenizer,
            max_seq_length=args.max_seq_length,
            doc_stride=args.doc_stride,
            max_query_length=args.max_query_length,
            is_training=False)
        
        # if True:
        if dist.get_rank()==0:
            dllogger.log(step="PARAMETER", data={"infer_start": True})
            dllogger.log(step="PARAMETER", data={"eval_samples": len(eval_examples)})
            dllogger.log(step="PARAMETER", data={"eval_features": len(eval_features)})
            dllogger.log(step="PARAMETER", data={"predict_batch_size": args.predict_batch_size})

        all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)
        all_example_index = torch.arange(all_input_ids.size(0), dtype=torch.long)

        eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_example_index)
        
        # Run prediction for full data
        eval_sampler = SequentialSampler(eval_data)
        eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.predict_batch_size)

        infer_start = time.time()


        model.eval()
        all_results = []
        # if True:
        if dist.get_rank() == 0:
            dllogger.log(step="PARAMETER", data={"eval_start": True})
        
        for input_ids, input_mask, segment_ids, example_indices in tqdm(eval_dataloader, desc="Evaluating", disable=args.disable_progress_bar):
            if len(all_results) % 1000 == 0 and dist.get_rank()==0:
                dllogger.log(step="PARAMETER", data={"sample_number": len(all_results)})
            input_ids = input_ids.to(device)
            input_mask = input_mask.to(device)
            segment_ids = segment_ids.to(device)
            with torch.no_grad():
                batch_start_logits, batch_end_logits = model(input_ids, segment_ids, input_mask)
            for i, example_index in enumerate(example_indices):
                start_logits = batch_start_logits[i].detach().cpu().tolist()
                end_logits = batch_end_logits[i].detach().cpu().tolist()
                eval_feature = eval_features[example_index.item()]
                unique_id = int(eval_feature.unique_id)
                all_results.append(RawResult(unique_id=unique_id,
                                            start_logits=start_logits,
                                            end_logits=end_logits))

        time_to_infer = time.time() - infer_start
        output_prediction_file = os.path.join(args.output_dir, "predictions.json")
        output_nbest_file = os.path.join(args.output_dir, "nbest_predictions.json")

        answers, nbest_answers = get_answers(eval_examples, eval_features, all_results, args)
        with open(output_prediction_file, "w") as f:
            f.write(json.dumps(answers, indent=4) + "\n")
        with open(output_nbest_file, "w") as f:
            f.write(json.dumps(nbest_answers, indent=4) + "\n")

        # output_null_log_odds_file = os.path.join(args.output_dir, "null_odds.json")
        # write_predictions(eval_examples, eval_features, all_results,
        #                   args.n_best_size, args.max_answer_length,
        #                   args.do_lower_case, output_prediction_file,
        #                   output_nbest_file, output_null_log_odds_file, args.verbose_logging,
        #                   args.version_2_with_negative, args.null_score_diff_threshold)

        if args.do_eval and is_main_process():
            import sys
            import subprocess
            eval_out = subprocess.check_output([sys.executable, args.eval_script,
                                              args.predict_file, args.output_dir + "/predictions.json"])
            scores = str(eval_out).strip()
            exact_match = float(scores.split(":")[1].split(",")[0])
            f1 = float(scores.split(":")[2].split("}")[0])
    
    if args.do_train:
        gpu_count = n_gpu
        if torch.distributed.is_initialized():
            gpu_count = torch.distributed.get_world_size()

        if args.max_steps == -1:
            # if True:
            if dist.get_rank()==0:
                dllogger.log(step=tuple(), data={"e2e_train_time": time_to_train,
                                             "training_sequences_per_second": len(train_features) * args.num_train_epochs / time_to_train,
                                             "final_loss": final_loss})
        else:
            # if True:
            if dist.get_rank()==0:
                dllogger.log(step=tuple(), data={"e2e_train_time": time_to_train,
                                             "training_sequences_per_second": args.train_batch_size * args.gradient_accumulation_steps \
                                              * args.max_steps * gpu_count / time_to_train,
                                              "final_loss": final_loss})
    
    if args.do_predict and is_main_process():
        # if True:
        if dist.get_rank()==0:
            dllogger.log(step=tuple(), data={"e2e_inference_time": time_to_infer,
                                                 "inference_sequences_per_second": len(eval_features) / time_to_infer})
    
    if args.do_eval and is_main_process():
        # if True:
        if dist.get_rank()==0:
            dllogger.log(step=tuple(), data={"exact_match": exact_match, "F1": f1})
    
    
    
def accuracy(output, target):
    # get the index of the max log-probability
    pred = output.max(1, keepdim=True)[1]
    return pred.eq(target.view_as(pred)).cpu().float().mean()

  
def first_second_copy_optimizer_async(optimizer):
    torch.cuda.set_device(dist.get_rank())
        
    if optimizer.state == {}:
        return
    elif cuda_stream_optimizer_dict_avg=={} or cuda_stream_optimizer_dict_avg_sq=={}:
        for tensor, momentum in optimizer.state.items():
            cuda_stream_optimizer_dict_avg[tensor] =torch.cuda.Stream()
            cuda_stream_optimizer_dict_avg_sq[tensor] =torch.cuda.Stream()
    
    numel = 0
    for tensor, momentum in optimizer.state.items():
        
        cuda_stream_optimizer_dict_avg[tensor].synchronize()
        
        with torch.cuda.stream(cuda_stream_optimizer_dict_avg[tensor]):
            exp_avg_numel = momentum['exp_avg'].numel()
            numel+=exp_avg_numel
            parameter_tensor_cpu = momentum['exp_avg'].to('cpu', non_blocking=True)
            cpu_optimizer_array_avg.append(parameter_tensor_cpu)

        # self.cuda_stream_optimizer_dict_avg_sq[tensor].synchronize()
        # with torch.cuda.stream(self.optimizer_stream_2):
        with torch.cuda.stream(cuda_stream_optimizer_dict_avg_sq[tensor]): 
            exp_avg_sq_numel = momentum['exp_avg_sq'].numel()
            numel+=exp_avg_sq_numel
            parameter_tensor_cpu = momentum['exp_avg_sq'].to('cpu', non_blocking=True)
            
            cpu_optimizer_array_avg_sq.append(parameter_tensor_cpu)
    
    if dist.get_rank()==0:
        print('Optimizer Numel = ',  numel)
        

if __name__ == '__main__':
    cuda_stream_model_dict ={}
    cuda_stream_optimizer_dict_avg ={}
    cuda_stream_optimizer_dict_avg_sq ={}
    
    model_clone = {}
    from collections import deque

    cpu_optimizer_array_avg = deque()
    cpu_optimizer_array_avg_sq = deque()
    cpu_model_array = deque()
        
    is_clone = False
    
    time_to_train = 0
    final_loss = None
    
    parser = get_argument_parser()

    deepspeed.init_distributed(dist_backend='nccl')

    # Include DeepSpeed configuration arguments
    parser = deepspeed.add_config_arguments(parser)

    args = parser.parse_args()
    args.local_rank = int(os.environ['LOCAL_RANK'])
    args.train_batch_size = int(args.train_batch_size /
                                args.gradient_accumulation_steps)


    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    
    
    args.fp16 = args.fp16 or args.amp
    
    args.cuda = not args.no_cuda and torch.cuda.is_available()
    #allreduce_batch_size = args.per_gpu_train_batch_size * args.batches_per_allreduce
    
    init_time =time.time()   
    
    
    
    torch.manual_seed(args.seed)
    # if True:
    if dist.get_rank()==0:
        print("\n ^^^^^^^000000000000^^^^^^^^^^^ args.seed  ", args.seed)


    if args.cuda:
    
       torch.cuda.set_device(dist.get_rank())
       torch.cuda.manual_seed(args.seed)
    

    ##########################
    device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    
    # device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    # n_gpu = torch.cuda.device_count()
    
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        # torch.distributed.init_process_group(backend='nccl', init_method='env://')
        n_gpu = 1

    if is_main_process():
        Path(os.path.dirname(args.json_summary)).mkdir(parents=True, exist_ok=True)
        dllogger.init(backends=[dllogger.JSONStreamBackend(verbosity=dllogger.Verbosity.VERBOSE,
                                                           filename=args.json_summary),
                                dllogger.StdOutBackend(verbosity=dllogger.Verbosity.VERBOSE, step_format=format_step)])
    else:
        dllogger.init(backends=[])
    
    # if True:
    if dist.get_rank()==0:
        dllogger.metadata("e2e_train_time", {"unit": "s"})
        dllogger.metadata("training_sequences_per_second", {"unit": "sequences/s"})
        dllogger.metadata("final_loss", {"unit": None})
        dllogger.metadata("e2e_inference_time", {"unit": "s"})
        dllogger.metadata("inference_sequences_per_second", {"unit": "sequences/s"})
        dllogger.metadata("exact_match", {"unit": None})
        dllogger.metadata("F1", {"unit": None})
        
        print("device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
                                device, n_gpu, bool(args.local_rank != -1), args.fp16))
        dllogger.log(step="PARAMETER", data={"Config": [str(args)]})

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
            args.gradient_accumulation_steps))

    args.train_batch_size = args.train_batch_size // args.gradient_accumulation_steps
    
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    
    # if True:
    if dist.get_rank()==0:
        dllogger.log(step="PARAMETER", data={"SEED": args.seed})

    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_predict:
        raise ValueError("At least one of `do_train` or `do_predict` must be True.")

    if args.do_train:
        if not args.train_file:
            raise ValueError(
                "If `do_train` is True, then `train_file` must be specified.")
    if args.do_predict:
        if not args.predict_file:
            raise ValueError(
                "If `do_predict` is True, then `predict_file` must be specified.")

    if os.path.exists(args.output_dir) and os.listdir(args.output_dir) and args.do_train and os.listdir(args.output_dir)!=['logfile.txt']:
        print("WARNING: Output directory {} already exists and is not empty.".format(args.output_dir), os.listdir(args.output_dir))
    if not os.path.exists(args.output_dir) and is_main_process():
        os.makedirs(args.output_dir)
        
    if dist.get_rank()==0:
        print('init_time = ', time.time()-init_time)
    
    init_tokenizer = time.time()
    
    tokenizer = BertTokenizer(args.vocab_file, do_lower_case=args.do_lower_case, max_len=512) # for bert large
    # tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=args.do_lower_case)

    train_examples = None
    num_train_optimization_steps = None
    if args.do_train:
        train_examples = read_squad_examples(
            input_file=args.train_file, is_training=True, version_2_with_negative=args.version_2_with_negative)
        num_train_optimization_steps = int(
            len(train_examples) / args.train_batch_size / args.gradient_accumulation_steps) * args.num_train_epochs
        if args.local_rank != -1:
            num_train_optimization_steps = num_train_optimization_steps // torch.distributed.get_world_size()

    # Prepare model
    config = modeling.BertConfig.from_json_file(args.config_file)
    # Padding for divisibility by 8
    if config.vocab_size % 8 != 0:
        config.vocab_size += 8 - (config.vocab_size % 8)
    
    if dist.get_rank()==0:
        print('init_tokenizer = ', time.time() - init_tokenizer)
    
    load_model_state_dict_time =time.time()
    
    model = modeling.BertForQuestionAnswering(config)
    # model = modeling.BertForQuestionAnswering.from_pretrained(args.bert_model,
                # cache_dir=os.path.join(str(PYTORCH_PRETRAINED_BERT_CACHE), 'distributed_{}'.format(args.local_rank)))
    # if True:
    if dist.get_rank()==0:
        dllogger.log(step="PARAMETER", data={"loading_checkpoint": True})
    
    if dist.get_rank()==0:
        print('load_model_state_dict_time = ', time.time() - load_model_state_dict_time)
    
    load_optimizer_time = time.time()
    
    # if True:
    if dist.get_rank()==0:
        dllogger.log(step="PARAMETER", data={"loaded_checkpoint": True})
    
    model.to(device)
    num_weights = sum([p.numel() for p in model.parameters() if p.requires_grad])
    
    # if True:
    if dist.get_rank()==0:
        dllogger.log(step="PARAMETER", data={"model_weights_num":num_weights})

    # Prepare optimizer
    param_optimizer = list(model.named_parameters())

    # hack to remove pooler, which is not used
    # thus it produce None grad that break apex
    param_optimizer = [n for n in param_optimizer if 'pooler' not in n[0]]

    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]
    
    if args.do_train:
        if args.fp16:
            try:
                from apex.optimizers import FusedAdam
            except ImportError:
                raise ImportError(
                    "Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")
            optimizer = FusedAdam(optimizer_grouped_parameters,
                                  lr=args.learning_rate,
                                  bias_correction=False)

            if args.loss_scale == 0:
                model, optimizer = amp.initialize(model, optimizer, opt_level="O2", keep_batchnorm_fp32=False,
                                                      loss_scale="dynamic")
            else:
                model, optimizer = amp.initialize(model, optimizer, opt_level="O2", keep_batchnorm_fp32=False, loss_scale=args.loss_scale)
            if args.do_train:
                scheduler = LinearWarmUpScheduler(optimizer, warmup=args.warmup_proportion, total_steps=num_train_optimization_steps)

        else:
            optimizer = BertAdam(optimizer_grouped_parameters,
                                    lr=args.learning_rate,
                                    warmup=args.warmup_proportion,
                                    t_total=num_train_optimization_steps)
        
        if dist.get_rank() == 0:
            print('load_optimizer_time = ', time.time() - load_optimizer_time)
        
        init_hvd_time = time.time()
    
    
    import asym_lib as asym_lib
    model, optimizer, _, _ = asym_lib.initialize(
        args=args,
        model=model,
        model_parameters=optimizer_grouped_parameters,
        dist_init_required=True)

    
    

    if dist.get_rank()==0:
        print('init_hvd_time =', time.time() - init_hvd_time) 
    
    load_data_time =time.time()
    
    # share_memory_count = torch.tensor(0).share_memory_()
    from multiprocessing import shared_memory

    if dist.get_rank()==0:
        logger.info("+++++++++++++++++++ train train train +++++++ dist.get_rank() = %d", dist.get_rank())
    
    
    # Train!    
    if args.do_train:
        if args.cache_dir is None:
            cached_train_features_file = args.train_file + '_{0}_{1}_{2}_{3}'.format(
                list(filter(None, args.bert_model.split('/'))).pop(), str(args.max_seq_length), str(args.doc_stride),
                str(args.max_query_length))
        else:
            cached_train_features_file = args.cache_dir.strip('/') + '/' + args.train_file.split('/')[-1] + '_{0}_{1}_{2}_{3}'.format(
                list(filter(None, args.bert_model.split('/'))).pop(), str(args.max_seq_length), str(args.doc_stride),
                str(args.max_query_length))

        train_features = None
        try:
            with open(cached_train_features_file, "rb") as reader:
                train_features = pickle.load(reader)
        except:
            train_features = convert_examples_to_features(
                examples=train_examples,
                tokenizer=tokenizer,
                max_seq_length=args.max_seq_length,
                doc_stride=args.doc_stride,
                max_query_length=args.max_query_length,
                is_training=True)

            if not args.skip_cache and is_main_process():
                # if True:
                if dist.get_rank()==0:
                    dllogger.log(step="PARAMETER", data={"Cached_train features_file": cached_train_features_file})
                with open(cached_train_features_file, "wb") as writer:
                    pickle.dump(train_features, writer)
        
        # if True:
        if dist.get_rank()==0:
            dllogger.log(step="PARAMETER", data={"train_start": True})
            dllogger.log(step="PARAMETER", data={"training_samples": len(train_examples)})
            dllogger.log(step="PARAMETER", data={"training_features": len(train_features)})
            dllogger.log(step="PARAMETER", data={"train_batch_size":args.train_batch_size})
            dllogger.log(step="PARAMETER", data={"steps":num_train_optimization_steps})
        
        all_input_ids = torch.tensor([f.input_ids for f in train_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in train_features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in train_features], dtype=torch.long)
        all_start_positions = torch.tensor([f.start_position for f in train_features], dtype=torch.long)
        all_end_positions = torch.tensor([f.end_position for f in train_features], dtype=torch.long)
        train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids,
                                   all_start_positions, all_end_positions)
        
        # if args.local_rank == -1:
        #     train_sampler = RandomSampler(train_data)
        # else:
        #     train_sampler = DistributedSampler(train_data)
        # train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size * n_gpu)
        
        # set batch size 
        # args.train_batch_size = args.train_batch_size * max(1, n_gpu)
        args.train_batch_size = args.train_batch_size
        
        
        
        train_sampler = RandomSampler(train_data) if dist.get_world_size()==0 else DistributedSampler(train_data, 
                                                                                           num_replicas = dist.get_world_size(), 
                                                                                           rank=dist.get_rank())
        # train_dataloader = DataLoader(train_data, 
        #                               sampler = train_sampler, 
        #                               batch_size = args.train_batch_size)
       
        kwargs = {'num_workers': 4, 'pin_memory': True} if args.cuda else {}
        # When supported, use 'forkserver' to spawn dataloader workers instead of 'fork' to prevent
        # # issues with Infiniband implementations that are not fork-safe
        if (kwargs.get('num_workers', 0) > 0 and hasattr(mp, '_supports_context') and
            mp._supports_context and 'forkserver' in mp.get_all_start_methods()):
            kwargs['multiprocessing_context'] = 'forkserver'
        
        train_dataloader = DataLoader(
            train_data,
            batch_size=args.train_batch_size,
            sampler=train_sampler,
            **kwargs)
    
    if dist.get_rank()==0:
        print('load_data_time = ', time.time() - load_data_time)
    state_time = time.time()
    
    resume_from_epoch = 0
    
    _state_dict_cpu = {}
    # numel_count = 0
    # for key, value in model.state_dict().items():
    #     t_cpu = torch.zeros(value.numel(), device='cpu', dtype=value.dtype, requires_grad=False)
    #     _state_dict_cpu[key] = t_cpu
    #     numel_count += value.numel()
    # _state_dict_gpu_flatten = torch.zeros(numel_count, device=value.device, dtype=value.dtype, requires_grad=False)
    # 

    if dist.get_rank() == 0:
        
        print('optimizer.state_dict() = ', optimizer.state_dict().keys())
        
        pass

    # torchsnapshot
    progress = torchsnapshot.StateDict(current_epoch=0)
    # torchsnapshot: define app state
    app_state: Dict[str, Stateful] = {
        "rng_state": torchsnapshot.RNGState(),
        "model": model,
        # "optim": optimizer,
        "progress": progress,
    }
    snapshot: Optional[Snapshot] = None
    
    checkpoint_save_work_dir = 'bert'

    full_train()
    
    dllogger.flush()















