# The MIT License (MIT)
# © 2024 templar.tech

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
# fmt: off

# Standard library
import sys
import time
import random
import asyncio
import argparse
import threading
import os
from contextlib import contextmanager
from time import perf_counter

# Third party
import torch
import numpy as np
import bittensor as bt
from torch.optim import SGD
from transformers import LlamaForCausalLM
from torch.optim.lr_scheduler import (
    CosineAnnealingWarmRestarts,
    LinearLR,
    SequentialLR,
)

# Local
import tplr

# GPU optimizations.
torch.manual_seed(42)
torch.cuda.manual_seed_all(42)
np.random.seed(42)
random.seed(42)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True

@contextmanager
def timer(name: str, wandb_obj=None, step=None):
    start = perf_counter()
    yield
    duration = perf_counter() - start
    tplr.logger.debug(f"{name} took {duration:.2f}s")
    if wandb_obj and step is not None:
        wandb_obj.log({f"validator/{name}": duration}, step=step)

class Validator:
    @staticmethod
    def config():
        parser = argparse.ArgumentParser(description='Validator script')
        parser.add_argument('--netuid', type=int, default=268, help='Bittensor network UID.')
        parser.add_argument('--project', type=str, default='templar', help='Wandb project.')
        parser.add_argument('--device', type=str, default='cuda', help='Device to use for training')
        parser.add_argument('--debug', action='store_true', help='Enable debug logging')
        parser.add_argument('--trace', action='store_true', help='Enable trace logging')
        parser.add_argument('--use_wandb', action='store_true', help='Use Weights and Biases for logging')
        parser.add_argument('--peers', type=int, nargs='+', default=[], help='List of UIDs to peer with')
        bt.subtensor.add_args(parser)
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)
        config = bt.config(parser)
        if config.debug:
            tplr.debug()
        if config.trace:
            tplr.trace()
        return config
    
    def __init__(self):
        tplr.logger.debug("Starting initialization...")
        
        # Init config and load hparams
        self.config = Validator.config()
        self.hparams = tplr.load_hparams()
        
        # Init bittensor objects
        self.wallet = bt.wallet(config=self.config)
        self.subtensor = bt.subtensor(config=self.config)
        self.metagraph = self.subtensor.metagraph(self.config.netuid)
        if self.wallet.hotkey.ss58_address not in self.metagraph.hotkeys:
            tplr.logger.error(f'\n\t[bold]The wallet {self.wallet} is not registered on subnet: {self.metagraph.netuid}[/bold]')
            sys.exit()
        self.uid = self.metagraph.hotkeys.index(self.wallet.hotkey.ss58_address)
        
        # Init model with hparams config
        self.model = LlamaForCausalLM(self.hparams.model_config)
        self.model.to(self.config.device)
        self.tokenizer = self.hparams.tokenizer
        
        # Init compression
        self.transformer = tplr.compress.TransformDCT(
            self.model, 
            target_chunk=self.hparams.target_chunk
        )
        self.compressor = tplr.compress.CompressDCT()
        
        # Init optimizer and momentum
        self.optimizer = SGD(self.model.parameters(), lr=self.hparams.learning_rate)
        self.momentum = {}
        self.xshapes = {}
        self.totalks = {}
        for n, p in self.model.named_parameters():
            self.momentum[n] = torch.zeros_like(p)
            _, _, xshape, totalk = self.compressor.compress(
                self.transformer.encode(self.momentum[n]), 
                self.hparams.topk_compression
            )
            self.xshapes[n] = xshape
            self.totalks[n] = totalk

        # Set up scheduler setup
        warmup_scheduler = LinearLR(
            self.optimizer,
            start_factor=0.1,
            end_factor=1.0,
            total_iters=250,
        )
        cosine_scheduler = CosineAnnealingWarmRestarts(
            self.optimizer,
            T_0=10000,
            T_mult=2,
            eta_min=self.hparams.learning_rate * 0.1
        )
        self.scheduler = SequentialLR(
            self.optimizer,
            schedulers=[warmup_scheduler, cosine_scheduler],
            milestones=[250]
        )

        # Init comms with required chain management args
        self.comms = tplr.comms.Comms(
            wallet=self.wallet,
            save_location='/tmp',
            key_prefix='model',
            config=self.config,
            netuid=self.config.netuid,
            metagraph=self.metagraph,
            hparams=self.hparams,
            uid=self.uid, 
        )


        self.bucket = self.comms.get_own_bucket()
        self.comms.try_commit(self.wallet, self.bucket)
        self.comms.fetch_commitments()
        
        
        # Init state params
        self.stop_event = asyncio.Event()
        self.current_block = self.subtensor.block
        self.current_window = int(self.current_block / self.hparams.blocks_per_window)
        self.comms.current_window = self.current_window 
        self.sync_window = self.current_window

        # Init scores and tracking
        self.scores = torch.zeros(self.metagraph.n, dtype=torch.float32)
        self.moving_avg_scores = torch.zeros(self.metagraph.n, dtype=torch.float32) 
        self.ma_alpha = 0.95  # Moving average decay factor
        self.evaluated_uids = set()  # Track which UIDs we've seen

        # Add step tracking
        self.global_step = 0
        self.window_step = 0
        self.eval_count = 0  # Track number of evaluations
        
        # Initialize WandB
        self.wandb = tplr.initialize_wandb(
            run_prefix='V',
            uid=self.uid,
            config=self.config,
            group='validator',
            job_type='validation'
        )

        # Initialize peers
        self.peers = []
        self.eval_peers = []


    async def run(self):
        # Load Peers
        if not self.config.peers:
            self.peers = self.comms.peers
            tplr.logger.info(f'Filtered gather peers with buckets: {self.peers}')
        else:
            self.peers = self.config.peers
        if self.uid not in self.peers:
            self.peers.append(self.uid)

        self.comms.commitments = self.comms.get_commitments_sync()
        self.comms.update_peers_with_buckets()
        tplr.logger.info(f"Loaded commitments: {self.comms.commitments.keys()}")

        # Try to load latest checkpoint
        result = await self.comms.get_latest_checkpoint()
        if result:
            checkpoint_data, window = result
            try:
                # Load state dicts from dictionary and move to device
                self.model.load_state_dict({k: v.to(self.config.device) for k,v in checkpoint_data['model_state_dict'].items()})
                self.model.to(self.config.device)
                
                # Move optimizer state to device
                for state in self.optimizer.state.values():
                    for k, v in state.items():
                        if torch.is_tensor(v):
                            state[k] = v.to(self.config.device)
                            
                self.optimizer.load_state_dict(checkpoint_data['optimizer_state_dict'])
                self.scheduler.load_state_dict(checkpoint_data['scheduler_state_dict'])
                self.momentum = checkpoint_data['momentum']
                self.global_step = checkpoint_data['global_step']
                
                # Update optimizer and scheduler steps to match
                self.optimizer._step_count = self.global_step  
                self.scheduler.last_epoch = self.global_step
                
                tplr.logger.info(f"Loaded checkpoint from window {window}, global_step={self.global_step}")
            except KeyError as e:
                tplr.logger.error(f"Invalid checkpoint format: missing key {e}")
            except Exception as e:
                tplr.logger.error(f"Failed to load checkpoint: {e}")
        else:
            tplr.logger.info("No valid checkpoints found, starting from scratch")
            self.global_step = 0
            self.model.to(self.config.device)
    

        # Start block listener
        self.loop = asyncio.get_running_loop()
        self.listener = threading.Thread(
            target=self.block_listener, 
            args=(self.loop,), 
            daemon=True
        ).start()
        self.comms.start_commitment_fetcher()
        self.comms.start_background_tasks()
        # self.comms.track_active_peers()

        while True:
            step_window = self.current_window

            tplr.logger.info(f'Step window: {step_window}, Scheduler epoch: {self.scheduler.last_epoch}, Global step: {self.global_step}')
            # 1. Wait for validator offset - single wait loop
            while self.sync_window >= (self.current_window - self.hparams.validator_offset):
                tplr.logger.info(f'Waiting for validator window offset, synced: {self.sync_window}, current:{self.current_window}, offset:{self.hparams.validator_offset}')
                await asyncio.sleep(12)
            tplr.logger.info(f'Step window: {step_window}, Scheduler epoch: {self.scheduler.last_epoch}, Global step: {self.global_step}')
            # 2. Process one window at a time
            self.sync_window += 1
            step_window = self.sync_window + 1
            tplr.logger.info(f'Processing window: {self.sync_window} current: {self.current_window}')

            self.comms.update_peers_with_buckets()
            # Update local references
            self.peers = self.comms.peers
            self.eval_peers = self.comms.eval_peers

            tplr.logger.info(f'Current gather peers: {self.peers}')
            tplr.logger.info(f'Current evaluation peers: {self.eval_peers}')

            # 3. Gather gradients from peers, but do not apply them yet
            with timer("gather_gradients", self.wandb, self.global_step):
                gather_result = await self.comms.gather(
                    state_dict=None,
                    my_uid=self.uid,
                    uids=self.peers,
                    window=step_window,
                    key='gradient',
                    timeout=5,
                    device=self.config.device,
                    local=False,
                    stale_retention=10,
                    global_step=self.global_step,
                )

            # # Save original model parameters
            original_params = {n: p.clone() for n, p in self.model.named_parameters()}

            # Evaluate selected miner before applying gathered gradients
            eval_uid = random.choice(self.eval_peers)
            tplr.logger.info(f'Evaluating uid: {eval_uid}')

            # Get individual miner's gradient
            eval_result = await self.comms.get(
                uid=str(eval_uid),
                window=step_window,
                key='gradient',
                timeout=30,
                local=False,
                stale_retention=10
            )

            if eval_result is None:
                tplr.logger.info(f"No gradient received from UID {eval_uid}. Skipping evaluation.")
                continue

            # Load evaluation data
            pages = await tplr.dataset.DatasetLoader.next_pages(
                offset=self.sync_window,
                n_pages=self.hparams.pages_per_window,
                seed=eval_uid
            )
            loader = await tplr.dataset.DatasetLoader.create(
                batch_size=self.hparams.batch_size,
                sequence_length=self.hparams.sequence_length,
                pages_info=pages,
                tokenizer=self.tokenizer
            )

            state_dict, _ = eval_result

            # Compute initial loss before applying the gradient
            self.model.train()
            self.optimizer.zero_grad()  # Zero gradients at start
            self.model.zero_grad()
            loss_before = 0.0
            n_batches = 0

            with torch.no_grad():
                for i, batch in enumerate(loader):
                    input_ids = torch.tensor(batch, dtype=torch.long).to(self.model.device)
                    labels = input_ids.clone()
                    labels = torch.where(labels == self.tokenizer.pad_token_id, -100, labels)
                    outputs = self.model(input_ids=input_ids, labels=labels)
                    loss_before += outputs.loss.item()
                    n_batches += 1
                    del input_ids, labels, outputs
                    torch.cuda.empty_cache()

            loss_before_per_batch = loss_before / n_batches if n_batches > 0 else 0
            tplr.logger.info(f'Loss before: {loss_before_per_batch}')

            # Before applying the gradient
            self.optimizer.zero_grad()
            self.model.zero_grad()

            for n, p in self.model.named_parameters():
                idxs_key = n + 'idxs'
                vals_key = n + 'vals'
                idxs = state_dict.get(idxs_key, None)
                vals = state_dict.get(vals_key, None)

                if idxs is not None and vals is not None:
                    # Move indices and values to validator's device
                    idxs = idxs.to(self.config.device)
                    vals = vals.to(self.config.device)
                    
                    # Decode the gradient
                    grad = self.transformer.decode(
                        self.compressor.decompress(
                            p.to(self.config.device),  # Ensure parameter is on correct device
                            idxs,
                            vals,
                            self.xshapes[n],
                            self.totalks[n],
                            # median=False
                        )
                    ).to(self.config.device)  # Ensure final gradient is on correct device

                    # Assign the gradient to p.grad
                    if p.grad is None:
                        p.grad = grad
                    else:
                        p.grad.copy_(grad)
                    p.grad.sign_()

                    p.data.sub_(grad, alpha = self.scheduler.get_last_lr()[0] ) 

                    
            # Compute loss after applying the gradient
            loss_after = 0.0
            n_batches = 0
            with torch.no_grad():
                for i, batch in enumerate(loader):
                    input_ids = torch.tensor(batch, dtype=torch.long).to(self.model.device)
                    labels = input_ids.clone()
                    labels = torch.where(labels == self.tokenizer.pad_token_id, -100, labels)
                    outputs = self.model(input_ids=input_ids, labels=labels)
                    loss_after += outputs.loss.item()
                    n_batches += 1
                    del input_ids, labels, outputs
                    torch.cuda.empty_cache()

            loss_after_per_batch = loss_after / n_batches if n_batches > 0 else 0
            tplr.logger.info(f'Loss after: {loss_after_per_batch}')

            # Calculate loss improvement
            loss_improvement = loss_before_per_batch - loss_after_per_batch
            tplr.logger.info(f'Loss improvement: {loss_improvement}')

            # Revert model parameters to original state
            for n, p in self.model.named_parameters():
                p.data.copy_(original_params[n])

            # Update scores
            relative_improvement = loss_improvement / loss_before_per_batch if loss_before_per_batch > 0 else 0.0
            tplr.logger.info(f"Relative improvement: {relative_improvement:.4f}")
            score = relative_improvement
            # Add the evaluated UID to the set
            self.evaluated_uids.add(eval_uid)

            # Update scores and moving averages
            self.scores[eval_uid] = score
            self.moving_avg_scores[eval_uid] = self.ma_alpha * self.moving_avg_scores[eval_uid] + (1 - self.ma_alpha) * score

            # Calculate weights - only positive moving averages get weights
            weights = torch.zeros_like(self.moving_avg_scores)
            evaluated_mask = torch.zeros_like(self.moving_avg_scores, dtype=torch.bool)
            evaluated_mask[list(self.evaluated_uids)] = True

            # Only consider positive moving averages for weight calculation
            positive_mask = (self.moving_avg_scores > 0) & evaluated_mask
            evaluated_scores = self.moving_avg_scores * positive_mask

            total_score = evaluated_scores.sum()
            if total_score > 0:
                weights[positive_mask] = evaluated_scores[positive_mask] / total_score

            # Log only evaluated UIDs
            tplr.logger.info('Updated scores for evaluated UIDs:')
            for uid in self.evaluated_uids:
                tplr.logger.info(f'UID {uid}:')
                tplr.logger.info(f'  - Last score: {self.scores[uid]}')
                tplr.logger.info(f'  - Moving avg score: {self.moving_avg_scores[uid]:.4f}')
                tplr.logger.info(f'  - Weight: {weights[uid]:.4f}')

            # Only set weights periodically
            if self.sync_window % self.hparams.windows_per_weights == 0:
                with timer("set_weights", self.wandb, self.global_step):
                    self.subtensor.set_weights(
                        wallet=self.wallet,
                        netuid=self.config.netuid,
                        uids=self.metagraph.uids,
                        weights=weights,
                        wait_for_inclusion=False,
                        wait_for_finalization=False,
                    )

            # 10. Log metrics and cleanup
            del loader, pages  # Explicit cleanup of dataset objects
            torch.cuda.empty_cache()

            # Log metrics for all evaluated UIDs
            valid_score_indices = torch.nonzero(self.scores > 0).squeeze().view(-1)
            for uid_i in valid_score_indices:
                uid = uid_i.item()
                self.wandb.log({
                    f"validator/scores/{uid}": self.scores[uid_i].item(),
                    f"validator/moving_avg_scores/{uid}": self.moving_avg_scores[uid_i].item(),
                    f"validator/weights/{uid}": weights[uid_i].item(),
                }, step=self.global_step)

            # Log evaluation metrics
            self.wandb.log({
                "validator/loss/before": loss_before_per_batch,
                "validator/loss/after": loss_after_per_batch,
                "validator/loss/improvement": score,
                "validator/network/block": self.current_block,
                "validator/network/window": self.sync_window,
                "validator/network/step": self.global_step,
                "validator/network/evaluated_uids": len(self.evaluated_uids),
                "validator/optimizer/learning_rate": self.scheduler.get_last_lr()[0],
                "validator/network/active_miners": len(valid_score_indices),
                "validator/scores/mean": self.scores[valid_score_indices].mean().item(),
                "validator/moving_avg_scores/mean": self.moving_avg_scores[valid_score_indices].mean().item()
            }, step=self.global_step)

            # Checkpoints
            if self.global_step % self.hparams.checkpoint_frequency == 0:
                tplr.logger.info(f"Creating checkpoint at global_step {self.global_step}")

                # Create CPU copy of the checkpoint data to avoid GPU memory competition
                checkpoint_data = {
                    'model_state_dict': {k: v.cpu().clone() for k, v in self.model.state_dict().items()},
                    'optimizer_state_dict': {k: v.cpu().clone() if torch.is_tensor(v) else v 
                                           for k, v in self.optimizer.state_dict().items()},
                    'scheduler_state_dict': self.scheduler.state_dict(),
                    'momentum': {k: v.cpu().clone() for k, v in self.momentum.items()},
                    'global_step': self.global_step
                }

                async def _save():
                    start_time = time.time()
                    try:
                        # Use a separate thread for CPU-intensive serialization
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, lambda: torch.save(checkpoint_data, '/tmp/temp_checkpoint.pt'))
                        
                        await self.comms.put(
                            state_dict=checkpoint_data,
                            uid=str(self.uid),
                            window=self.current_window,
                            key='checkpoint',
                            global_step=self.global_step,
                            local=False
                        )
                        elapsed_time = time.time() - start_time
                        tplr.logger.info(f"Successfully saved checkpoint at global_step {self.global_step} (took {elapsed_time:.2f}s)")
                        
                        self.wandb.log({
                            "validator/save_time": elapsed_time,
                            "validator/global_step": self.global_step,
                        }, step=self.global_step)
                        
                    except Exception as e:
                        tplr.logger.error(f"Failed to save checkpoint: {e}")
                    finally:
                        # Cleanup temp file
                        if os.path.exists('/tmp/temp_checkpoint.pt'):
                            os.remove('/tmp/temp_checkpoint.pt')

                asyncio.create_task(_save())

            # Now apply the gathered gradients
            if gather_result is not None:
                # Update self.global_step based on the maximum global_step received
                max_global_step = max(gather_result.global_steps + [self.global_step])
                if max_global_step > self.global_step:
                    tplr.logger.info(f"Updating global_step from {self.global_step} to {max_global_step}")
                    self.global_step = max_global_step
                    self.optimizer._step_count = self.global_step
                    self.scheduler.last_epoch = self.global_step

                with timer("update_model_with_gathered", self.wandb, self.global_step):
                    self.optimizer.zero_grad()
                    self.model.zero_grad()
                    
                    for n, p in self.model.named_parameters():
                        idxs_key = n + 'idxs'
                        vals_key = n + 'vals'
                        idxs = getattr(gather_result.state_dict, idxs_key, None)
                        vals = getattr(gather_result.state_dict, vals_key, None)
                        if idxs is not None and vals is not None:
                            # Ensure idx and val are lists of tensors
                            if not isinstance(idxs, (list, tuple)):
                                idxs = [idxs]
                            if not isinstance(vals, (list, tuple)):
                                vals = [vals]
                            
                            new_grad = self.transformer.decode(
                                self.compressor.batch_decompress(
                                    p.to(self.config.device),
                                    idxs,
                                    vals,
                                    self.xshapes[n],
                                    self.totalks[n],
                                    # median=True
                                )
                            )
                            # Set recomputed gathered gradient.
                            if p.grad is None:
                                p.grad = new_grad
                            else:
                                p.grad.copy_(new_grad)
                            # Sign-SGD
                            p.grad.sign_()
                        else:
                            tplr.logger.info(f"Gradient data missing for parameter {n}, skipping.")

                    # **Perform optimization step**
                    self.optimizer.step()
                    self.scheduler.step()
                    torch.cuda.empty_cache()

                    # Increment global_step
                    self.global_step += 1
                    self.optimizer._step_count = self.global_step  # Ensure optimizer's step count matches

                    # Log steps to wandb
                    self.wandb.log({
                        "validator/global_step": self.global_step,
                        "validator/optimizer_step_count": self.optimizer._step_count,
                        "validator/scheduler_last_epoch": self.scheduler.last_epoch,
                    }, step=self.global_step)

    def block_listener(self, loop):
        def handler(event, _u, _s):
            self.current_block = int(event['header']['number'])
            new_window = int(self.current_block / self.hparams.blocks_per_window)
            if new_window != self.current_window:
                self.current_window = new_window
                self.comms.current_window = self.current_window  # Synchronize comms current_window
        while not self.stop_event.is_set():
            try:
                bt.subtensor(config=self.config).substrate.subscribe_block_headers(handler)
                break
            except Exception:
                time.sleep(1)

if __name__ == "__main__":
    asyncio.run(Validator().run())
