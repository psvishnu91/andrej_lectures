"""Train nanoGPT

Usage::

    # train mode
    python train_gpt.py
    # prediction mode: Set the model at :data:`LOAD_MODEL_CKPT_PATH`
    python train_gpt.py predict
"""
import os
import os.path
import sys
import functools as ft
import time
import torch
import torch.nn.functional as F
import torch.utils.data as data_utils

import torch.utils.tensorboard as tb
from typing import Dict
from typing import Tuple
from dataclasses import dataclass
from tqdm import tqdm

from torch import nn

import gpt

#######################################################################################
# Imports for multi GPU training
import torch.multiprocessing as mp

# Contains init and destroy process group helpers
import torch.distributed

# Contains `DistributedSampler``
import torch.utils.data.distributed

# Contains `DistributedDataParallel` class which does the heavy lifting
import torch.nn.parallel

#######################################################################################


st_time = time.time()

#######################################################################################
# Constants
#######################################################################################
USE_MULTIGPU = False

INPUT_FL = "input.txt"
# device = "mps" if torch.backends.mps.is_available() else "cpu"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Running on device: {DEVICE}")
RUN_ID = f"{int(time.time())}"
# Set this to the path to load
# "logging/checkpoints/sml_transformer_2023-02-06_00_23_38/4500.pt"
LOAD_MODEL_CKPT_PATH = None  # "logging/checkpoints/final_model/6900.pt"
START_IT = 4800


@dataclass(frozen=True)
class TrainConfig:

    batch_sz: int
    save_every: int
    learning_rate: float
    eval_freq: int
    max_iters: int
    eval_sz: int = 2000
    train_frac: float = 0.9
    checkpoint_folder: str = f"logging/checkpoints/{RUN_ID}/"


@dataclass(frozen=True)
class TransformerConfig:
    block_sz: int
    # if you update this also update mlp_hidden_dim
    embed_dim: int
    # embed_dim * 4
    mlp_hidden_dim: int
    num_attn_heads: int
    dropout_frac: float
    # number of decoder transformer blocks stacked one on top of another
    num_blocks: int


@dataclass(frozen=True)
class HyperParams:
    transformer_cfg: TransformerConfig
    train_cfg: TrainConfig


FULL_MODEL_HYPERPARAMS = HyperParams(
    train_cfg=TrainConfig(
        batch_sz=256,
        save_every=300,
        learning_rate=3e-4,
        eval_freq=100,
        max_iters=5001,
    ),
    transformer_cfg=TransformerConfig(
        block_sz=256,
        embed_dim=384,
        mlp_hidden_dim=384 * 4,
        num_attn_heads=6,
        dropout_frac=0.2,
        num_blocks=6,
    ),
)
TEST_HYPERPARAMS = HyperParams(
    train_cfg=TrainConfig(
        batch_sz=32,
        save_every=10_000,  # don't save
        learning_rate=1e-3,
        eval_freq=500,
        max_iters=6001,
    ),
    transformer_cfg=TransformerConfig(
        block_sz=8,
        embed_dim=32,
        mlp_hidden_dim=32 * 4,
        num_attn_heads=2,
        dropout_frac=0.2,
        num_blocks=1,
    ),
)
HYPERPARAMS = TEST_HYPERPARAMS

#######################################################################################
#  Main logic function
#######################################################################################


def main(rank, world_size, is_predict_mode):
    tb_writer = setup_tensorboard()
    kwargs = dict(
        is_predict_mode=is_predict_mode,
        device=rank,
        tb_writer=tb_writer,
        use_multigpu=USE_MULTIGPU,
    )
    if not USE_MULTIGPU:
        gpt_wrapper(**kwargs)
    else:
        try:
            setup_ddp(rank=rank, world_size=world_size)
            gpt_wrapper(**kwargs)
        except Exception as e:
            torch.distributed.destroy_process_group()
            raise e


def gpt_wrapper(
    is_predict_mode,
    device,
    tb_writer,
    use_multigpu,
    input_fl=INPUT_FL,
    hyperparams=HYPERPARAMS,
    load_model_ckpt_path=LOAD_MODEL_CKPT_PATH,
):
    torch.manual_seed(1338)
    train_cfg, transformer_cfg = hyperparams.train_cfg, hyperparams.transformer_cfg
    train_data, val_data, vocab_sz, ixtoc = load_input_file(
        input_fl=input_fl, train_frac=train_cfg.train_frac
    )
    train_dl, val_dl = get_dataloaders(
        train_data=train_data,
        val_data=val_data,
        block_sz=transformer_cfg.block_sz,
        batch_sz=train_cfg.batch_sz,
        device=device,
        use_multigpu=use_multigpu,
    )
    model = build_model(
        vocab_sz=vocab_sz,
        transformer_cfg=transformer_cfg,
        device=device,
        load_model_ckpt_path=load_model_ckpt_path,
        use_multigpu=use_multigpu,
    )
    tb_writer.add_graph(model, next(iter(train_dl))[0])
    print("\nExamples before training the model")
    print_example_kwargs = dict(
        model=model,
        max_len=10_000 if is_predict_mode else 300,
        block_size=transformer_cfg.block_sz,
        ixtoc=ixtoc,
        device=device,
        num_examples=2,
    )
    gpt.print_examples(**print_example_kwargs)  # type: ignore
    if is_predict_mode:
        print("In prediction mode. Exiting!")
        return
    optimiser = torch.optim.AdamW(params=model.parameters(), lr=train_cfg.learning_rate)
    # Train the model
    print("Training the transformer model")
    train_model(
        model=model,
        train_dl=train_dl,
        num_iters=train_cfg.max_iters,
        optimizer=optimiser,
        loss_calc_freq=train_cfg.eval_freq,
        losses={"train": [], "val": []},  # not used, see tensorboard instead
        eval_loss_fn=ft.partial(
            eval_loss, train_dl=train_dl, val_dl=val_dl, eval_sz=train_cfg.eval_sz
        ),
        save_every=train_cfg.save_every,
        device=device,
        checkpoint_folder=train_cfg.checkpoint_folder,
        use_multigpu=use_multigpu,
        tb_writer=tb_writer,
    )
    print("\n\nExamples after training the model")
    gpt.print_examples(**print_example_kwargs)  # type: ignore
    el_time = time.time() - st_time
    print(f"Time elapsed: {el_time:,}s {el_time//60:,}m")
    tb_writer.close()


#######################################################################################
# SETUP
#######################################################################################
def setup_tensorboard():
    # logging setup
    for folder in ("tb", "checkpoints"):
        os.makedirs(f"logging/{folder}/{RUN_ID}/")
    tb_writer = tb.SummaryWriter(log_dir=f"logging/tb/{RUN_ID}")
    print(f"{HYPERPARAMS}")
    return tb_writer


def setup_ddp(rank, world_size):
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = "45678"
    torch.distributed.init_process_group(
        backend="gloo", rank=rank, world_size=world_size
    )


#######################################################################################
# Load data
#######################################################################################
def load_input_file(input_fl, train_frac):
    with open(input_fl, encoding="utf-8") as infile:
        input_txt = infile.read()
    vocab = sorted(set(input_txt))
    vocab_sz = len(vocab)
    ctoix = {char: ix for ix, char in enumerate(vocab)}
    ixtoc = {ix: char for ix, char in enumerate(vocab)}
    print("Vocab:", vocab)
    print(f"Num characters: {len(input_txt):,}\nExample text:\n")
    print(input_txt[:1000])
    print("-----------------------------------------------------------")
    data = torch.tensor(encode(txt=input_txt, ctoix=ctoix), dtype=torch.long)
    n = int(train_frac * len(data))  # first 90% will be train, rest val
    train_data = data[:n]
    val_data = data[n:]
    return train_data, val_data, vocab_sz, ixtoc


def encode(txt, ctoix):
    return [ctoix[char] for char in txt]


@dataclass
class DecoderDataset(data_utils.Dataset):

    block_sz: int
    device: str
    data: torch.Tensor

    def __post_init__(self):
        super().__init__()
        self.len = len(self.data) - self.block_sz - 1

    def __len__(self):
        return self.len

    def __getitem__(self, index):
        i = index
        x = self.data[i : i + self.block_sz]
        y = self.data[i + 1 : i + self.block_sz + 1]
        return x.to(self.device), y.to(self.device)


def get_dataloaders(
    train_data: torch.Tensor,
    val_data: torch.Tensor,
    block_sz: int,
    batch_sz: int,
    device: str,
    use_multigpu: bool,
) -> Tuple[data_utils.DataLoader, data_utils.DataLoader]:
    ds_kwargs = dict(block_sz=block_sz, device=device)
    dl_kwargs = dict(batch_size=batch_sz, pin_memory=True, shuffle=False)
    train_ds = DecoderDataset(data=train_data, **ds_kwargs)  # type: ignore
    # We want different shards of training data to each gpu and since we are using
    # a sampler we want to turn off shuffle.
    if use_multigpu:
        train_dl = data_utils.DataLoader(
            dataset=train_ds,
            sampler=torch.utils.data.distributed.DistributedSampler(train_ds),
            **dl_kwargs,
        )
    else:
        train_dl = data_utils.DataLoader(dataset=train_ds, sampler=None, **dl_kwargs)
    # The GPUs need not get a unique copy of validation data so we don't distributed
    # sampler here.
    val_dl = data_utils.DataLoader(
        dataset=DecoderDataset(data=val_data, **ds_kwargs), **dl_kwargs  # type: ignore
    )
    return train_dl, val_dl


def get_batch_helper(split, batch_sz, train_data, val_data, block_sz, device):
    # generate a small batch of data of inputs x and targets y
    data = train_data if split == "train" else val_data
    ix = torch.randint(len(data) - block_sz, (batch_sz,))
    x = torch.stack([data[i : i + block_sz] for i in ix])
    y = torch.stack([data[i + 1 : i + block_sz + 1] for i in ix])
    x, y = x.to(device), y.to(device)
    return x, y


#######################################################################################
# Training loop
#######################################################################################


def build_model(vocab_sz, transformer_cfg, device, load_model_ckpt_path, use_multigpu):
    # Define the model
    print("Creating the transformer model")
    model = gpt.Transformer(
        embed_dim=transformer_cfg.embed_dim,
        vocab_sz=vocab_sz,
        block_size=transformer_cfg.block_sz,
        num_attn_head=transformer_cfg.num_attn_heads,
        mlp_hidden_dim=transformer_cfg.mlp_hidden_dim,
        dropout_frac=transformer_cfg.dropout_frac,
        num_blocks=transformer_cfg.num_blocks,
        device=device,
    ).to(device)
    # print the model architecture
    for nm, p in model.named_parameters():
        print(nm, p.shape)
    # print the number of parameters in the model
    print(sum(p.numel() for p in model.parameters()) / 1e6, "M parameters")
    if load_model_ckpt_path is not None:
        print(f"Loading model from checkpoint: {load_model_ckpt_path}")
        model.load_state_dict(
            torch.load(load_model_ckpt_path, map_location=torch.device("cpu"))
        )
    else:
        print("No model checkpoint passed. Training from scratch.")
    if use_multigpu:
        # If we are using multiple GPU we are going to make a copy of this model in
        # every gpu.
        model = torch.nn.parallel.DistributedDataParallel(
            module=model, device_ids=[device]
        )
    # model = torch.compile(model=model)
    return model


#######################################################################################
# Training loop
#######################################################################################


def train_model(
    model: nn.Module,
    train_dl: data_utils.DataLoader,
    num_iters: int,
    optimizer,
    eval_loss_fn,
    checkpoint_folder,
    save_every,
    device,
    use_multigpu,
    tb_writer,
    losses: Dict[str, list],  # train -> [], val -> []
    loss_calc_freq=100,
):
    prev_val_loss = 1e5
    for i, (Xi, Yi) in (pbar := tqdm(zip(range(num_iters), train_dl), total=num_iters)):
        loss = eval_model_loss(model=model, X=Xi, Ytrue=Yi)
        model.zero_grad()
        loss.backward()
        optimizer.step()
        # Metric tracking
        if not ((i == num_iters - 1) or (i % loss_calc_freq == 0)):
            continue
        eval_losses = eval_loss_fn(model=model)
        tb_writer.add_scalars(
            main_tag="loss", tag_scalar_dict=eval_losses, global_step=i
        )
        loss_msg = ""
        for split, loss in eval_losses.items():
            losses[split].append(loss)
            loss_msg += f"{split} loss: {loss:.4f} "
        pbar.set_description(loss_msg)
        if ((i == num_iters - 1) or (i % save_every == 0)) and (
            eval_losses["val"] < prev_val_loss
        ):
            _checkpoint(
                model=model,
                checkpoint_folder=checkpoint_folder,
                it=i + START_IT,
                use_multigpu=use_multigpu,
                device=device,
            )
        prev_val_loss = eval_losses["val"]


def _checkpoint(model, checkpoint_folder, it, use_multigpu, device):
    if device in {"cpu", 0}:
        # Don't save unless you're CPU or the first GPU.
        # All GPUs possess an identical copy and we don't want each process to
        # save a checkpoint.
        return
    fl = os.path.join(checkpoint_folder, f"{it}.pt")
    msg = f"Iteration {it}: Checkpointing model at {fl}"
    print(msg)
    print(msg)
    state_dict = model.module.state_dict() if use_multigpu else model.state_dict()
    torch.save(state_dict, f=fl)


@torch.no_grad()
def eval_loss(model, train_dl, val_dl, eval_sz):
    model.eval()
    out = {}
    # Dataloader issues out x, y of length batch_sz but we want eval_sz. Find the
    #   number of batch_sz we need and vstack them.
    # next(train_dl) -> x, y
    batch_sz = next(iter(train_dl))[0].shape[0]
    # slightly more than eval sz but 🤷
    stacks = (eval_sz // batch_sz) + 1
    for split, dl in [("train", train_dl), ("val", val_dl)]:
        Xbatches, Ybatches = [], []
        for _ in range(stacks):
            Xb, Yb = next(iter(dl))
            Xbatches.append(Xb)
            Ybatches.append(Yb)
        Xeval, Yeval = torch.vstack(Xbatches), torch.vstack(Ybatches)
        out[split] = eval_model_loss(model=model, X=Xeval, Ytrue=Yeval).item()
    model.train()
    return out


def eval_model_loss(model, X, Ytrue):
    return calc_loss(Ypred=model(X=X), Ytrue=Ytrue)


def calc_loss(Ypred, Ytrue):
    """
    Ypred dim: Batch_sz x block_sz x vocab_sz
    Ytrue dim: Batch_sz x block_sz
    """
    return F.cross_entropy(input=_flatten_y(Ypred), target=Ytrue.view(-1))


def _flatten_y(Y):
    B, T, C = Y.shape
    return Y.view(B * T, C)


@torch.no_grad()
def calc_split_loss(model, data_splits, split):
    model.eval()
    X, Ytrue = data_splits[split]
    loss = model(X=X, target=Ytrue).item()
    model.train()
    return loss


#######################################################################################
# Boiler plate main function
#######################################################################################


def _parse_args():
    if len(sys.argv) > 1:
        return sys.argv[1].startswith("predict")
    else:
        return False


if __name__ == "__main__":
    is_predict_mode = _parse_args()
    if USE_MULTIGPU:
        world_size = torch.cuda.device_count()
        mp.spawn(main, args=(world_size, is_predict_mode), nprocs=world_size)
    else:
        main(world_size=1, rank=DEVICE, is_predict_mode=is_predict_mode)
