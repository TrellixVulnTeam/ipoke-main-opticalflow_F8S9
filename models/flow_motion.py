from models.modules.INN.INN import UnsupervisedMaCowTransformer3
from models.modules.INN.loss import FlowLoss
from models.opticalFlow.models import FlowVAE
from models.first_stage_motion_model import SpadeCondMotionModel
from models.second_stage_video import PokeMotionModel
from functools import partial
import wandb
import matplotlib.pyplot as plt
import torch
import pytorch_lightning as pl
from torch.nn import functional as F
from collections import OrderedDict
import numpy as np
import cv2
import yaml
from glob import glob

class PokeMotionModelFixed(PokeMotionModel):
    def __init__(self, config, dirs):
        super().__init__(config, dirs)

    def train(self, mode: bool):
        """ avoid pytorch lighting auto set trian mode """
        return super().train(False)

    def state_dict(self, destination, prefix, keep_vars):
        """ avoid pytorch lighting auto save params """
        destination = OrderedDict()
        destination._metadata = OrderedDict()
        return destination

    def setup(self, device: torch.device):
        self.freeze()

class FlowVAEFixed(FlowVAE):
    def __init__(self, config):
        super.__init__(config)

    def train(self, mode: bool):
        """ avoid pytorch lighting auto set trian mode """
        return super().train(False)

    def state_dict(self, destination, prefix, keep_vars):
        """ avoid pytorch lighting auto save params """
        destination = OrderedDict()
        destination._metadata = OrderedDict()
        return destination

    def setup(self, device: torch.device):
        self.freeze()


class FlowMotion(pl.LightningModule):

    def __init__(self, config):

        self.dirs = 'dummy'
        super(FlowMotion, self).__init__()
        self.config = config
        self.VAE = FlowVAEFixed(config).eval()
        self.INN = UnsupervisedMaCowTransformer3(self.config["architecture"])
        self.motion_model = PokeMotionModelFixed

        ckpt_path = '/export/scratch3/ablattma/ipoke/second_stage/ckpt/plants_64/0/'
        ckpt_path = glob(ckpt_path + '.*ckpt')
        assert len(ckpt_path == 1), 'multiple checkpoints found for PokeMotionModel (i.e. second stage)'
        ckpt_path = ckpt_path[0]

        config_path = 'config/pretrained_models/plants_64.yaml'
        with open(config_path, 'r') as stream:
            config_motion = yaml.load(stream)

        self.motion_model.load_from_checkpoint(ckpt_path, map_location="cpu", config=config_motion, strict=False, dirs=self.dirs)

        self.loss_func = FlowLoss()

        checkpoint = torch.load(config["checkpoint"]["VAE"], map_location='cpu')
        new_state_dict = OrderedDict()
        for key, value in checkpoint['state_dict'].items():
            new_key = key[6:] # trimming keys by removing "model."
            new_state_dict[new_key] = value

        m, u = self.VAE.load_state_dict(new_state_dict, strict=False)
        assert len(m) == 0, "VAE state_dict is missing pretrained params"
        del checkpoint
        self.VAE.setup(self.device)
        self.motion_model.setup(self.device)

    def video_enc(self, batch):
        motion, _, _ = self.motion.enc_motion(batch.transpose(1, 2))
        return motion

    def forward(self, batch):
        self.INN.train()
        out_hat = self.video_enc(batch['images'].cuda())
        out, logdet = self.forward_density(batch['flow'].cuda())
        loss, loss_dict = self.loss_func(out, logdet) + F.mse_loss(out, out_hat)
        print(out.shape)
        print()
        print(out_hat.shape)
        return loss

    # def on_fit_start(self) -> None:
    #     self.VAE.setup(self.device)

    def forward_sample(self, batch, n_samples=1, n_logged_imgs=1):
        image_samples = []

        with torch.no_grad():

            for n in range(n_samples):
                flow_input, _, _ = self.VAE.encoder(batch)
                flow_input = torch.randn_like(flow_input).detach()
                out = self.INN(flow_input, reverse=True)
                out = self.VAE.decoder([out], del_shape=False)
                image_samples.append(out[:n_logged_imgs])

        return image_samples

    def forward_density(self, batch):
        with torch.no_grad():
            encv, _, _ = self.VAE.encoder(batch)

        out, logdet = self.INN(encv.detach(), reverse=False)

        return out, logdet


    def configure_optimizers(self):
        trainable_params = [{"params": self.INN.parameters(), "name": "flow"}, ]

        optimizer = torch.optim.Adam(trainable_params, lr=self.config["training"]['lr'], betas=(0.9, 0.999),
                                     weight_decay=self.config["training"]['weight_decay'], amsgrad=True)

        return [optimizer]

#     def training_step(self,batch, batch_idx):
#         self.INN.train()
#         out, logdet = self.forward_density(batch)
#
#         loss, loss_dict = self.loss_func(out,logdet)
#
#         self.log_dict(loss_dict,prog_bar=True,on_step=True,logger=False)
#         self.log_dict({"train/"+key: loss_dict[key] for key in loss_dict},logger=True,on_epoch=True,on_step=True)
#         self.log("global_step",self.global_step)
#
#         lr = self.optimizers().param_groups[0]["lr"]
#         self.log("learning_rate",lr,on_step=True,on_epoch=False,prog_bar=True,logger=True)
#
#         if self.global_step % self.config["logging"]["log_train_prog_at"] == 0:
#             self.INN.eval()
#             n_samples = self.config["logging"]["n_samples"]
#             n_logged_imgs = self.config["logging"]["n_log_images"]
#             with torch.no_grad():
#                 image_samples = self.forward_sample(batch,n_samples-1,n_logged_imgs)
#                 tgt_imgs = batch[:n_logged_imgs]
#                 image_samples.insert(0, tgt_imgs)
#
#                 enc, *_ = self.VAE.encoder(tgt_imgs)
#                 rec = self.VAE.decoder([enc], del_shape=False)
#                 image_samples.insert(1, rec)
#                 sample, _ = self.INN(enc)
#                 returned = self.INN(sample, reverse=True)
#                 dec_returned = self.VAE.decoder([returned], del_shape=False)
#                 image_samples.insert(2, dec_returned)
#
#                 captions = ["target", "rec","flow_rec"] + ["sample"] * (n_samples-1)
#             img = fig_matrix(image_samples, captions)
#
#             self.logger.experiment.history._step=self.global_step
#             self.logger.experiment.log({"Image Grid train set":wandb.Image(img,
#                                                                 caption=f"Image Grid train @ it #{self.global_step}")}
#                                         ,step=self.global_step, commit=False)
#             if self.global_step % (self.config["logging"]["log_train_prog_at"]*10) == 0:
#                 img = color_fig(image_samples, captions)
#                 self.logger.experiment.log({"Image sample": wandb.Image(img, caption=f"Image sample @ it #{self.global_step}")}
#                                            , step=self.global_step, commit=False)
#
#         return loss
#
#     def validation_step(self, batch, batch_id):
#
#         with torch.no_grad():
#             out, logdet = self.forward_density(batch)
#
#             loss, loss_dict = self.loss_func(out, logdet)
#
#             self.log_dict({"val/" + key: loss_dict[key] for key in loss_dict}, logger=True, on_epoch=True)
#
#         return {"loss": loss, "val-batch": batch, "batch_idx": batch_id, "loss_dict": loss_dict}
#
#     def on_train_batch_start(self, batch, batch_idx, dataloader_idx):
#         if self.apply_lr_scaling and self.global_step <= self.config["training"]["lr_scaling_max_it"]:
#             # adjust learning rate
#             lr = self.lr_scaling(self.global_step)
#             opt = self.optimizers()
#             for pg in opt.param_groups:
#                 pg["lr"] = lr
#
#         if self.custom_lr_decrease and self.global_step >= 500:
#             lr = self.lr_adaptation(self.global_step)
#             # self.console_logger.info(f'global step is {self.global_step}, learning rate is {lr}\n')
#             opt = self.optimizers()
#             for pg in opt.param_groups:
#                 pg["lr"] = lr
#
#
#
# def linear_var(
#     act_it, start_it, end_it, start_val, end_val, clip_min, clip_max
# ):
#     act_val = (
#         float(end_val - start_val) / (end_it - start_it) * (act_it - start_it)
#         + start_val
#     )
#     return np.clip(act_val, a_min=clip_min, a_max=clip_max)
if __name__ == '__main__':
    from data.datamodule import StaticDataModule

    import os
    os.environ["CUDA_VISIBLE_DEVICES"] = '5'

    with open('config/VAE_INN.yaml', 'r') as stream:
        config = yaml.safe_load(stream)
    flowmotion = FlowMotion(config).cuda()

    config_data = {'spatial_size': [64, 64],
                   'dataset': 'PlantDataset',
                   'max_frames': 10,
                   'batch_size': 16,
                   'n_workers': 1,
                   'yield_videos': True,
                   'split': 'official'}
    datakeys = ['images', 'flow']

    datamod = StaticDataModule(config_data, datakeys=datakeys)
    datamod.setup()
    for idx, batch in enumerate(datamod.train_dataloader()):
        flowmotion(batch)
        break
