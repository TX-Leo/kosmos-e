#!/usr/bin/env python3 -u
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import sys
sys.path.append( '.' )

import csv
import base64
from io import BytesIO

import unilm
import ast
import fileinput
import logging
import math
import os
import sys
import time
import re
import json
from argparse import Namespace
from collections import namedtuple
import random
from sklearn.preprocessing import KBinsDiscretizer

import numpy as np
import torch

from fairseq import checkpoint_utils, distributed_utils, options, tasks, utils
from fairseq.dataclass.configs import FairseqConfig
from fairseq.dataclass.utils import convert_namespace_to_omegaconf
from fairseq.token_generation_constraints import pack_constraints, unpack_constraints
from fairseq_cli.generate import get_symbols_to_strip_from_output

import sentencepiece as spm
from torchvision import transforms


from draw_box_on_image import *
#import gradio as gr

# store the image path for visualize
global_image_path = None
global_image_tensor = None

# store the question-answer pair 
global_qa_prompts = {}

# This is simple maximum entropy normalization performed in Inception paper
inception_normalize = transforms.Compose(
    [transforms.Normalize(mean=[0.48145466, 0.4578275, 0.40821073], std=[0.26862954, 0.26130258, 0.27577711])]
)

def square_transform(size=224):
    return transforms.Compose(
        [
            transforms.Resize((size, size), interpolation=transforms.InterpolationMode.BICUBIC),
            # transforms.Resize(size, interpolation=transforms.InterpolationMode.BICUBIC),
            # transforms.CenterCrop(size),
            transforms.ToTensor(),
            inception_normalize,
        ]
    )

def split_string(string, separators):
    """
    Function to split a given string based on a list of separators.

    Args:
    string (str): The input string to be split.
    separators (list): A list of separators to be used for splitting the string.

    Returns:
    A list containing the split string with separators included.
    """
    pattern = "|".join(re.escape(separator) for separator in separators) 
    result = re.split(f'({pattern})', string)  
    return [elem for elem in result if elem] 


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=os.environ.get("LOGLEVEL", "INFO").upper(),
    stream=sys.stdout,
)
logger = logging.getLogger("fairseq_cli.interactive")


Batch = namedtuple("Batch", "ids src_tokens src_lengths constraints img_src_tokens img_gpt_input_mask")
Translation = namedtuple("Translation", "src_str hypos pos_scores alignments")


def get_interactive_tokens_and_lengths(self, lines, encode_fn, tokenizer=None):
    """
    line format: [image]path<tab>text<tab>[image]path
    model input: `<s> <image> image hidden </image> My cat looking very dignified.</s>`
    """
    image_feature_length = self.args.image_feature_length
    bos_id = self.dictionary.bos()
    eos_id = self.dictionary.eos()
    boi_id = self.dictionary.index("<image>")
    eoi_id = self.dictionary.index("</image>")
    
    def convert_one_line(input_str):
        # TODO: input interleave image and text
        token = []
        img_src_token = []
        img_gpt_input_mask = []
        segments = input_str.split('<tab>')
        token.append(bos_id)
        img_gpt_input_mask.append(0)
        for i, segment in enumerate(segments):
            if segment.startswith('[image]'):
                image_path = segment[7:]
                # read image and transform to tensor
                image = Image.open(image_path).convert("RGB")
                # print(image)
                # update the global_path
                global global_image_path
                global_image_path = image_path
                image_tensor = square_transform(self.args.input_resolution)(image)
                img_src_token.append(image_tensor)
                global global_image_tensor
                global_image_tensor = image_tensor
                # token.extend([boi_id] + [boi_id] * image_feature_length + [eoi_id])
                token.extend([boi_id] + list(range(4, image_feature_length+4)) + [eoi_id])
                
                img_gpt_input_mask.extend([0] + [1] * image_feature_length + [0])
            else:
                special_tokens = [self.source_dictionary[idx] for idx in range(tokenizer.vocab_size(), 
                                                                               len(self.source_dictionary))]
                split_special_token_words = []
                split_resutls = split_string(segment, special_tokens)
                for string in split_resutls:
                    if string in special_tokens:
                        #print(f"dict-length({len(self.source_dictionary)}), substring {string} is a special token")
                        split_special_token_words.append(string)
                    else:
                        encode_tokens = tokenizer.encode(string, out_type=str)
                        #print(f"dict-length({len(self.source_dictionary)}), substring {string} is not a special token, tokenized into {encode_tokens}")
                        split_special_token_words.extend(encode_tokens)
                # pdb.set_trace()
                segment = ' '.join(split_special_token_words)
                
                text_tokens = self.source_dictionary.encode_line(
                    encode_fn(segment), add_if_not_exist=False
                ).tolist()
                
                text_tokens = text_tokens[:-1] # </s> in token
                token.extend(text_tokens)
                img_gpt_input_mask.extend([0] * (len(text_tokens))) # </s> in token
        token.append(eos_id)
        # img_gpt_input_mask = img_gpt_input_mask[:-1]
        assert len(token) == len(img_gpt_input_mask) + 1 
        token = torch.LongTensor(token)
        img_gpt_input_mask = torch.LongTensor(img_gpt_input_mask)
        img_src_token = torch.stack(img_src_token, dim=0)
        return token, img_src_token, img_gpt_input_mask
    
    tokens = []
    img_src_tokens = []
    img_gpt_input_masks = []
    for src_str in lines:
        #print(f"dict length: {len(self.source_dictionary)}")
        token, img_src_token, img_gpt_input_mask = convert_one_line(src_str)
        tokens.append(token)
        img_src_tokens.append(img_src_token)
        img_gpt_input_masks.append(img_gpt_input_mask)
    lengths = [t.numel() for t in tokens]
    
    return tokens, lengths, img_src_tokens, img_gpt_input_masks


def make_batches(lines, cfg, task, max_positions, encode_fn):
    def encode_fn_target(x):
        return encode_fn(x)

    if cfg.generation.constraints:
        # Strip (tab-delimited) contraints, if present, from input lines,
        # store them in batch_constraints
        batch_constraints = [list() for _ in lines]
        for i, line in enumerate(lines):
            if "\t" in line:
                lines[i], *batch_constraints[i] = line.split("\t")

        # Convert each List[str] to List[Tensor]
        for i, constraint_list in enumerate(batch_constraints):
            batch_constraints[i] = [
                task.target_dictionary.encode_line(
                    encode_fn_target(constraint),
                    append_eos=False,
                    add_if_not_exist=False,
                )
                for constraint in constraint_list
            ]

    if cfg.generation.constraints:
        constraints_tensor = pack_constraints(batch_constraints)
    else:
        constraints_tensor = None

    tokenizer = spm.SentencePieceProcessor()
    tokenizer.Load('/mnt/msranlp/shumma/data/16g/sentencepiece.bpe.model')
    tokens, lengths, img_src_tokens, img_gpt_input_mask = get_interactive_tokens_and_lengths(task, lines, encode_fn, tokenizer)

    itr = task.get_batch_iterator(
        dataset=task.build_dataset_for_caption_inference(
            tokens, lengths, img_src_tokens, img_gpt_input_mask, constraints=constraints_tensor
        ),
        max_tokens=cfg.dataset.max_tokens,
        max_sentences=cfg.dataset.batch_size,
        max_positions=max_positions,
        ignore_invalid_inputs=cfg.dataset.skip_invalid_size_inputs_valid_test,
    ).next_epoch_itr(shuffle=False)
    for batch in itr:
        ids = batch["id"]
        src_tokens = batch["net_input"]["src_tokens"]
        src_lengths = batch["net_input"]["src_lengths"]
        img_src_tokens = batch["net_input"]["img_src_tokens"]
        img_gpt_input_mask = batch["net_input"]["img_gpt_input_mask"]
        constraints = batch.get("constraints", None)

        yield Batch(
            ids=ids,
            src_tokens=src_tokens,
            src_lengths=src_lengths,
            img_src_tokens=img_src_tokens,
            img_gpt_input_mask=img_gpt_input_mask,
            constraints=constraints,
        )


def main(cfg: FairseqConfig):
    if isinstance(cfg, Namespace):
        cfg = convert_namespace_to_omegaconf(cfg)

    start_time = time.time()
    total_translate_time = 0

    utils.import_user_module(cfg.common)

    if cfg.interactive.buffer_size < 1:
        cfg.interactive.buffer_size = 1
    if cfg.dataset.max_tokens is None and cfg.dataset.batch_size is None:
        cfg.dataset.batch_size = 1

    assert (
        not cfg.generation.sampling or cfg.generation.nbest == cfg.generation.beam
    ), "--sampling requires --nbest to be equal to --beam"
    assert (
        not cfg.dataset.batch_size
        or cfg.dataset.batch_size <= cfg.interactive.buffer_size
    ), "--batch-size cannot be larger than --buffer-size"

    logger.info(cfg)

    # Fix seed for stochastic decoding
    if cfg.common.seed is not None and not cfg.generation.no_seed_provided:
        import numpy as np
        np.random.seed(cfg.common.seed)
        utils.set_torch_seed(cfg.common.seed)

    use_cuda = torch.cuda.is_available() and not cfg.common.cpu

    # Setup task, e.g., translation
    # pdb.set_trace()
    logger.info("Task: {}".format(cfg.task))
    task = tasks.setup_task(cfg.task)

    # Load ensemble
    overrides = ast.literal_eval(cfg.common_eval.model_overrides)
    logger.info("loading model(s) from {}".format(cfg.common_eval.path))
    models, _model_args = checkpoint_utils.load_model_ensemble(
        utils.split_paths(cfg.common_eval.path),
        arg_overrides=overrides,
        task=task,
        suffix=cfg.checkpoint.checkpoint_suffix,
        strict=(cfg.checkpoint.checkpoint_shard_count == 1),
        num_shards=cfg.checkpoint.checkpoint_shard_count,
    )

    # Set dictionaries
    src_dict = task.source_dictionary
    tgt_dict = task.target_dictionary

    # Optimize ensemble for generation
    for model in models:
        if model is None:
            continue
        if cfg.common.fp16:
            model.half()
        if use_cuda and not cfg.distributed_training.pipeline_model_parallel:
            model.cuda()
        model.prepare_for_inference_(cfg)

    # Initialize generator
    generator = task.build_generator(models, cfg.generation)

    # Handle tokenization and BPE
    tokenizer = task.build_tokenizer(cfg.tokenizer)
    bpe = task.build_bpe(cfg.bpe)

    def encode_fn(x):
        if tokenizer is not None:
            x = tokenizer.encode(x)
        if bpe is not None:
            x = bpe.encode(x)
        return x

    def decode_fn(x):
        if bpe is not None:
            x = bpe.decode(x)
        if tokenizer is not None:
            x = tokenizer.decode(x)
        return x

    # Load alignment dictionary for unknown word replacement
    # (None if no unknown word replacement, empty if no path to align dictionary)
    align_dict = utils.load_align_dict(cfg.generation.replace_unk)

    max_positions = utils.resolve_max_positions(
        task.max_positions(), *[model.max_positions() for model in models]
    )

    if cfg.generation.constraints:
        logger.warning(
            "NOTE: Constrained decoding currently assumes a shared subword vocabulary."
        )

    if cfg.interactive.buffer_size > 1:
        logger.info("Sentence buffer size: %s", cfg.interactive.buffer_size)
    logger.info("NOTE: hypothesis and token scores are output in base 2")
    logger.info("Type the input sentence and press return:")
    start_id = 0
    
    def generate_predictions(image_input, text_input, prompt_text_input):
        
        if image_input is None:
            user_image_path = None
        else:
            user_image_path = image_input
            # user_image_path = "/tmp/user_input_test_image.jpg"
            # image_input.save(user_image_path)
            
        if (image_input is not None) and (text_input is None):
            if prompt_text_input is None:
                inputs = f"[image]{user_image_path}<tab>"
            else:
                inputs = f"{prompt_text_input}<tab>[image]{user_image_path}<tab>"
        elif (image_input is None) and (text_input is not None):
            if prompt_text_input is None:
                inputs = text_input
            else:
                inputs = f"{prompt_text_input}<tab>{text_input}"
        elif (image_input is None) and (text_input is None):
            inputs = prompt_text_input
        else:
            if prompt_text_input is None:
                inputs = f"[image]{user_image_path}<tab>{text_input}"
            else:
                inputs = prompt_text_input + f"<tab>[image]{user_image_path}<tab>{text_input}"
        
        #print("inputs", inputs)
        inputs = [inputs,]
        
        results = []
        for batch in make_batches(inputs, cfg, task, max_positions, encode_fn):
            bsz = batch.src_tokens.size(0)
            src_tokens = batch.src_tokens
            src_lengths = batch.src_lengths
            img_src_tokens = batch.img_src_tokens
            img_gpt_input_mask = batch.img_gpt_input_mask
            constraints = batch.constraints
            if use_cuda:
                src_tokens = src_tokens.cuda()
                src_lengths = src_lengths.cuda()
                if constraints is not None:
                    constraints = constraints.cuda()

            sample = {
                "net_input": {
                    "src_tokens": src_tokens,
                    "src_lengths": src_lengths,
                    "img_src_tokens": img_src_tokens,
                    "img_gpt_input_mask": img_gpt_input_mask,
                },
            }
            translate_start_time = time.time()
            translations = task.inference_step(
                generator, models, sample, constraints=constraints
            )
            translate_time = time.time() - translate_start_time
            # total_translate_time += translate_time
            list_constraints = [[] for _ in range(bsz)]
            if cfg.generation.constraints:
                list_constraints = [unpack_constraints(c) for c in constraints]
            for i, (id, hypos) in enumerate(zip(batch.ids.tolist(), translations)):
                src_tokens_i = utils.strip_pad(src_tokens[i], tgt_dict.pad())
                constraints = list_constraints[i]
                results.append(
                    (
                        start_id + id,
                        src_tokens_i,
                        hypos,
                        {
                            "constraints": constraints,
                            "time": translate_time / len(translations),
                        },
                    )
                )

        # sort output to match input order
        for id_, src_tokens, hypos, info in sorted(results, key=lambda x: x[0]):
            src_str = ""
            if src_dict is not None:
                # print(src_tokens)
                # pdb.set_trace()
                src_str = src_dict.string(src_tokens, cfg.common_eval.post_process)
                #print("S-{}\t{}".format(id_, src_str))
                #print("W-{}\t{:.3f}\tseconds".format(id_, info["time"]))
                # for constraint in info["constraints"]:
                #     print(
                #         "C-{}\t{}".format(
                #             id_,
                #             tgt_dict.string(constraint, cfg.common_eval.post_process),
                #         )
                #     )

            # Process top predictions
            for hypo in hypos[: min(len(hypos), cfg.generation.nbest)]:
                # hypo_tokens, hypo_str, alignment = utils.post_process_prediction(
                hypo_tokens, hypo_str, alignment = post_process_prediction(
                    hypo_tokens=hypo["tokens"].int().cpu(),
                    src_str=src_str,
                    alignment=hypo["alignment"],
                    align_dict=align_dict,
                    tgt_dict=tgt_dict,
                    remove_bpe=cfg.common_eval.post_process,
                    extra_symbols_to_ignore=get_symbols_to_strip_from_output(generator),
                )
                detok_hypo_str = decode_fn(hypo_str)
                
                # show the results on the image
                # pdb.set_trace()
                response_str = detok_hypo_str.split('</image>')[-1].strip()
                text_input = text_input.strip()
                if text_input in response_str:
                    response_str = response_str[len(text_input):].strip()
                if global_image_path is not None:
                    basename = os.path.basename(global_image_path).split('.')[0]
                    vis_image, cleaned_text, box_list = visualize_results_on_image(global_image_path, response_str, task.args.location_bin_size, f"output/store_vis_results/show_box_on_{basename}.jpg", show=False)
                # if global_image_tensor is not None:
                #     basename = os.path.basename(global_image_path).split('.')[0]
                #     vis_image = visualize_results_on_image(global_image_tensor, response_str, task.args.location_bin_size, f"output/store_vis_results/show_box_on_{basename}.jpg", show=False)
                
                # import pudb;pu.db
                output = {"input": text_input, "response": cleaned_text, "box": []}
                for box in box_list:
                    phrase = box[0]
                    phrase_position = [box[1], box[2]]
                    box_position = [box[3], box[4], box[5], box[6]]
                    output["box"].append({"phrase":phrase, "phrase_position":phrase_position, "box_position":box_position})
                #print(json.dumps(output))

                score = hypo["score"] / math.log(2)  # convert to base 2
                # original hypothesis (after tokenization and BPE)
                #print("H-{}\t{}\t{}".format(id_, score, hypo_str))
                # detokenized hypothesis
                #print("D-{}\t{}\t{}".format(id_, score, detok_hypo_str))
                # print(
                #     "P-{}\t{}".format(
                #         id_,
                #         " ".join(
                #             map(
                #                 lambda x: "{:.4f}".format(x),
                #                 # convert from base e to base 2
                #                 hypo["positional_scores"].div_(math.log(2)).tolist(),
                #             )
                #         ),
                #     )
                # )
                
                # pdb.set_trace()
                if cfg.generation.print_alignment:
                    alignment_str = " ".join(
                        ["{}-{}".format(src, tgt) for src, tgt in alignment]
                    )
                    #print("A-{}\t{}".format(id_, alignment_str))
    
        return vis_image, str(inputs[0]), str(response_str), str(hypo["tokens"].int().cpu().tolist()), str(detok_hypo_str),cleaned_text
    
    # # read line from stdin
    # while True:
    #     line = sys.stdin.readline()
    #     if line.strip() == "exit":
    #         break
    #     segments = line.strip().split('<tab>')
    #     image_path = segments[0]
    #     text = segments[1]
    #     generate_predictions(image_path, text, None)

    # gradio interface layout
    # image_input = gr.inputs.Image(type="pil", label="Test Image (optional)")  
    # text_input = gr.inputs.Textbox(lines=2, label="Input text for test image (optional)", placeholder="You can input text here")
    # prompt_text_input = gr.inputs.Textbox(lines=4, label="Prompt text input (optional)", placeholder="[image]...image_path...<tab>...text...")
    
    # image_output = gr.outputs.Image(type="pil")
    # text_output0 = gr.outputs.Textbox(label="Your input")
    # text_output1 = gr.outputs.Textbox(label="Response")  
    # text_output2 = gr.outputs.Textbox(label="Original hypothesis")  
    # text_output3 = gr.outputs.Textbox(label="Detokenized hypothesis") 

    # iface = gr.Interface(
    #     fn=generate_predictions,
    #     inputs=[image_input, text_input, prompt_text_input],  
    #     outputs=[image_output, text_output0, text_output1, text_output2, text_output3],  
    #     title="Demo"
    # )

    # iface.launch(share=True, enable_queue=True)

    # logger.info(
    #     "Total time: {:.3f} seconds; translation time: {:.3f}".format(
    #         time.time() - start_time, total_translate_time
    #     )
    # )
    
    from data.cornell_evaluate import test_evaluate
    def parse_args():
        class DotDict:
            def __init__(self, dictionary):
                self.__dict__ = dictionary
            def __getattr__(self, attr):
                return self.__dict__.get(attr)
        args = DotDict({
            'dataset': 'cornell',
            'dataset_path': '/mnt/msranlpintern/dataset/cornell-v12/',
            'input_size': 224,
            'ds_rotate': 0.0,
            'augment': False,
            'use_depth': 1,
            'use_rgb': 1,
            'iou_threshold': 0.25,
            'dataloader_num': '08',
            'train_output_num': '01',
            'grasp_format': 'xya',
            'splited': False,
            'encoded': True,
            'vis': False,
            'instruction_type':"angle"
        })
        return args
    args = parse_args()
    test_evaluate(args,generate_predictions)

# changed from fairseq.utils.py
def post_process_prediction(
    hypo_tokens,
    src_str,
    alignment,
    align_dict,
    tgt_dict,
    remove_bpe=None,
    extra_symbols_to_ignore=None,
):
    hypo_str = tgt_dict.string(
        hypo_tokens, remove_bpe, extra_symbols_to_ignore=extra_symbols_to_ignore
    )
    if align_dict is not None:
        hypo_str = utils.replace_unk(
            hypo_str, src_str, alignment, align_dict, tgt_dict.unk_string()
        )
    if align_dict is not None or remove_bpe is not None:
        # Convert back to tokens for evaluating with unk replacement or without BPE
        # Note that the dictionary can be modified inside the method.
        hypo_tokens = tgt_dict.encode_line(hypo_str, add_if_not_exist=False)
    return hypo_tokens, hypo_str, alignment



def cli_main():
    parser = options.get_interactive_generation_parser()
    args = options.parse_args_and_arch(parser)
    distributed_utils.call_main(convert_namespace_to_omegaconf(args), main)


if __name__ == "__main__":
    cli_main()
