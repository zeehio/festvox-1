"""Trainining script for Tacotron speech synthesis model.

usage: train.py [options]

options:
    --data-root=<dir>         Directory contains preprocessed features.
    --preset=<json>              Path of preset parameters (json).
    --checkpoint-dir=<dir>    Directory where to save model checkpoints [default: checkpoints].
    --checkpoint-path=<name>  Restore model from checkpoint path if given.
    --hparams=<parmas>        Hyper parameters [default: ].
    --log-event-path=<dir>    Log Path [default: exp/log_tacotronOne]
    -h, --help                Show this help message and exit
"""
from docopt import docopt
from collections import defaultdict
import os
import re
os.environ["CUDA_VISIBLE_DEVICES"]='1,2'
# Use text & audio modules from existing Tacotron implementation.
import sys
from os.path import dirname, join

### This is not supposed to be hardcoded #####
FALCON_DIR = os.environ.get('FALCONDIR')
sys.path.append(FALCON_DIR)
##############################################
from utils.misc import *
from utils import audio
from utils.plot import plot_alignment
from tqdm import tqdm, trange


from models import TacotronOneSeqwise as Tacotron

import json

import torch
from torch.utils import data as data_utils
from torch.autograd import Variable
from torch import nn
from torch import optim
import torch.backends.cudnn as cudnn
from torch.utils import data as data_utils
import numpy as np

from os.path import join, expanduser

import librosa.display
from matplotlib import pyplot as plt
import sys
import os
import tensorboard_logger
from tensorboard_logger import *
from hparams_arctic import hparams, hparams_debug_string

# Default DATA_ROOT
DATA_ROOT = join(expanduser("~"), "tacotron", "training")

fs = hparams.sample_rate

global_step = 0
global_epoch = 0
use_cuda = torch.cuda.is_available()
if use_cuda:
    cudnn.benchmark = False




def train(model, train_loader, val_loader, optimizer,
          init_lr=0.002,
          checkpoint_dir=None, checkpoint_interval=None, nepochs=None,
          clip_thresh=1.0):
    model.train()
    if use_cuda:
        model = model.cuda()
    ##linear_dim = model.linear_dim
    linear_dim = 1025

    criterion = nn.L1Loss()
    #criterion_noavg = nn.L1Loss(reduction='none')

    global global_step, global_epoch
    while global_epoch < nepochs:
        running_loss = 0.
        for step, (x, input_lengths, mel, y) in tqdm(enumerate(train_loader)):
            # Decay learning rate
            current_lr = learning_rate_decay(init_lr, global_step)
            for param_group in optimizer.param_groups:
                param_group['lr'] = current_lr

            optimizer.zero_grad()

            # Feed data
            x, mel, y = Variable(x), Variable(mel), Variable(y)
            if use_cuda:
                x, mel, y = x.cuda(), mel.cuda(), y.cuda()
            #mel_outputs, linear_outputs, attn = model(
            #    x, mel)
            outputs,  r_, o_ = data_parallel_workaround(model, (x, mel))
            mel_outputs, linear_outputs, attn = outputs[0], outputs[1], outputs[2]

            # Loss
            mel_loss = criterion(mel_outputs, mel)
            n_priority_freq = int(3000 / (fs * 0.5) * linear_dim)
            linear_loss = 0.5 * criterion(linear_outputs, y) \
                + 0.5 * criterion(linear_outputs[:, :, :n_priority_freq],
                                  y[:, :, :n_priority_freq])
            loss = mel_loss + linear_loss

            if global_step > 0 and global_step % checkpoint_interval == 0:
                save_states(
                    global_step, mel_outputs, linear_outputs, attn, y,
                    None, checkpoint_dir)
                save_checkpoint(
                    model, optimizer, global_step, checkpoint_dir, global_epoch)

            # Update
            loss.backward()
            grad_norm = torch.nn.utils.clip_grad_norm(
                model.parameters(), clip_thresh)
            optimizer.step()

            # Logs
            log_value("loss", float(loss.item()), global_step)
            log_value("mel loss", float(mel_loss.item()), global_step)
            log_value("linear loss", float(linear_loss.item()), global_step)
            log_value("gradient norm", grad_norm, global_step)
            log_value("learning rate", current_lr, global_step)
            log_histogram("Last Linear Weights", model.last_linear.weight.detach().cpu(), global_step)
            global_step += 1
            running_loss += loss.item()

        averaged_loss = running_loss / (len(train_loader))
        log_value("loss (per epoch)", averaged_loss, global_epoch)
        print("Loss: {}".format(running_loss / (len(train_loader))))
        #sys.exit()

        global_epoch += 1


if __name__ == "__main__":
    args = docopt(__doc__)
    print("Command line args:\n", args)
    checkpoint_dir = args["--checkpoint-dir"]
    checkpoint_path = args["--checkpoint-path"]
    log_path = args["--log-event-path"]
    preset = args["--preset"]
    data_root = args["--data-root"]
    if data_root:
        DATA_ROOT = data_root

    hparams.parse(args["--hparams"])

    # Override hyper parameters
    if preset is not None:
        with open(preset) as f:
            hparams.parse_json(f.read())
    os.makedirs(checkpoint_dir, exist_ok=True)

    # Vocab size
    with open('etc/ids_phones.json') as  f:
       charids = json.load(f)

    charids = dict(charids)
    print(charids)

    feats_name = 'phones'
    X_train = CategoricalDataSource('fnames.train', 'etc/falcon_feats.desc', feats_name, 'festival/falcon_' + feats_name, charids)
    X_val = CategoricalDataSource('fnames.val', 'etc/falcon_feats.desc', feats_name,  feats_name, charids)

    feats_name = 'lspec'
    Y_train = FloatDataSource('fnames.train', 'etc/falcon_feats.desc', feats_name, 'festival/falcon_' + feats_name)
    Y_val = FloatDataSource('fnames.val', 'etc/falcon_feats.desc', feats_name, 'festival/falcon_' + feats_name)

    feats_name = 'mspec'
    Mel_train = FloatDataSource('fnames.train', 'etc/falcon_feats.desc', feats_name, 'festival/falcon_' + feats_name)
    Mel_val = FloatDataSource('fnames.val', 'etc/falcon_feats.desc', feats_name, 'festival/falcon_' + feats_name)

    # Dataset and Dataloader setup
    trainset = PyTorchDataset(X_train, Mel_train, Y_train)
    train_loader = data_utils.DataLoader(
        trainset, batch_size=hparams.batch_size,
        num_workers=hparams.num_workers, shuffle=True,
        collate_fn=collate_fn, pin_memory=hparams.pin_memory)

    valset = PyTorchDataset(X_val, Mel_val, Y_val)
    val_loader = data_utils.DataLoader(
        valset, batch_size=hparams.batch_size,
        num_workers=hparams.num_workers, shuffle=True,
        collate_fn=collate_fn, pin_memory=hparams.pin_memory)

    # Model
    model = Tacotron(n_vocab=1+ len(charids),
                     embedding_dim=256,
                     mel_dim=hparams.num_mels,
                     linear_dim=hparams.num_freq,
                     r=hparams.outputs_per_step,
                     padding_idx=hparams.padding_idx,
                     use_memory_mask=hparams.use_memory_mask,
                     )
    model = model.cuda()
    #model = DataParallelFix(model)

    optimizer = optim.Adam(model.parameters(),
                           lr=hparams.initial_learning_rate, betas=(
                               hparams.adam_beta1, hparams.adam_beta2),
                           weight_decay=hparams.weight_decay)

    # Load checkpoint
    if checkpoint_path:
        print("Load checkpoint from: {}".format(checkpoint_path))
        checkpoint = torch.load(checkpoint_path)
        model.load_state_dict(checkpoint["state_dict"])
        optimizer.load_state_dict(checkpoint["optimizer"])
        try:
            global_step = checkpoint["global_step"]
            global_epoch = checkpoint["global_epoch"]
        except:
            # TODO
            pass

    # Setup tensorboard logger
    tensorboard_logger.configure(log_path)

    print(hparams_debug_string())

    # Train!
    try:
        train(model, train_loader, val_loader, optimizer,
              init_lr=hparams.initial_learning_rate,
              checkpoint_dir=checkpoint_dir,
              checkpoint_interval=hparams.checkpoint_interval,
              nepochs=hparams.nepochs,
              clip_thresh=hparams.clip_thresh)
    except KeyboardInterrupt:
        save_checkpoint(
            model, optimizer, global_step, checkpoint_dir, global_epoch)

    print("Finished")
    sys.exit(0)
