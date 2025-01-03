import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from torch.nn import functional as F
import pandas as pd
import numpy as np
from tqdm import tqdm
import matplotlib.pyplot as plt

import lightning as L
from torch.optim.lr_scheduler import LambdaLR, CosineAnnealingLR, SequentialLR


def compute_closeness(sdf_pred, sdf_target):
    # Mean Absolute Error
    mae = F.l1_loss(sdf_pred, sdf_target.unsqueeze(1), reduction='mean')
    # Root Mean Square Error
    rmse = torch.sqrt(F.mse_loss(sdf_pred, sdf_target.unsqueeze(1), reduction='mean'))
    return mae, rmse

def finite_difference_smoothness_torch(points):
    dx = torch.diff(points, dim=0)
    dy = torch.diff(points, dim=1)
    dx = dx[:, :-1]  # Adjust dx to match the shape of dy
    dy = dy[:-1, :]  # Adjust dy to match the shape of dx
    smoothness = torch.sqrt(dx**2 + dy**2)
    return smoothness.mean()

def orthogonality_metric(z, rad_latent_dim):
    z_radius = z[:, :rad_latent_dim]
    z_original = z[:, rad_latent_dim:]
    
    Q_radius, R_radius = torch.linalg.qr(z_radius)
    Q_original, R_original = torch.linalg.qr(z_original)

    orthogonality_loss = torch.linalg.norm(Q_radius.T @ Q_original, ord=2)
    return orthogonality_loss



class Decoder(nn.Module):
    """
    Decoder from DeepSDF:
    https://github.com/facebookresearch/DeepSDF/blob/main/deep_sdf/networks.py
    """
    def __init__(
        self,
        latent_size,
        dims,
        dropout=None,
        dropout_prob=0.0,
        norm_layers=(),
        latent_in=(),
        weight_norm=False,
        xyz_in_all=None,
        use_tanh=False,
        latent_dropout=False,
    ):
        super(Decoder, self).__init__()

        def make_sequence():
            return []

        dims = [latent_size + 2] + dims + [1]

        self.num_layers = len(dims)
        self.norm_layers = norm_layers
        self.latent_in = latent_in
        self.latent_dropout = latent_dropout
        if self.latent_dropout:
            self.lat_dp = nn.Dropout(0.2)

        self.xyz_in_all = xyz_in_all
        self.weight_norm = weight_norm

        for layer in range(0, self.num_layers - 1):
            if layer + 1 in latent_in:
                out_dim = dims[layer + 1] - dims[0]
            else:
                out_dim = dims[layer + 1]
                if self.xyz_in_all and layer != self.num_layers - 2:
                    out_dim -= 2

            if weight_norm and layer in self.norm_layers:
                setattr(
                    self,
                    "lin" + str(layer),
                    nn.utils.weight_norm(nn.Linear(dims[layer], out_dim)),
                )
            else:
                setattr(self, "lin" + str(layer), nn.Linear(dims[layer], out_dim))

            if (
                (not weight_norm)
                and self.norm_layers is not None
                and layer in self.norm_layers
            ):
                setattr(self, "bn" + str(layer), nn.LayerNorm(out_dim))

        self.use_tanh = use_tanh
        if use_tanh:
            self.tanh = nn.Tanh()
        self.relu = nn.ReLU()

        self.dropout_prob = dropout_prob
        self.dropout = dropout
        self.th = nn.Tanh()

    # input: N x (L+3)
    def forward(self, input):
        xyz = input[:, -2:]

        if input.shape[1] > 2 and self.latent_dropout:
            latent_vecs = input[:, :-2]
            latent_vecs = F.dropout(latent_vecs, p=0.2, training=self.training)
            x = torch.cat([latent_vecs, xyz], 1)
        else:
            x = input

        for layer in range(0, self.num_layers - 1):
            lin = getattr(self, "lin" + str(layer))
            if layer in self.latent_in:
                x = torch.cat([x, input], 1)
            elif layer != 0 and self.xyz_in_all:
                x = torch.cat([x, xyz], 1)
            x = lin(x)
            # last layer Tanh
            if layer == self.num_layers - 2 and self.use_tanh:
                x = self.tanh(x)
            if layer < self.num_layers - 2:
                if (
                    self.norm_layers is not None
                    and layer in self.norm_layers
                    and not self.weight_norm
                ):
                    bn = getattr(self, "bn" + str(layer))
                    x = bn(x)
                x = self.relu(x)
                if self.dropout is not None and layer in self.dropout:
                    x = F.dropout(x, p=self.dropout_prob, training=self.training)

        if hasattr(self, "th"):
            x = self.th(x)

        return x
    
class Decoder_loss(nn.Module):
    def __init__(self, latent_dim, hidden_dim):
        super(Decoder_loss, self).__init__()

        self.model = nn.Sequential(
            nn.Linear(latent_dim, hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim, hidden_dim * 2),
            nn.BatchNorm1d(hidden_dim * 2),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim, 1)
        )

    def forward(self, z):
        return self.model(z)


class AE(nn.Module):
    def __init__(self, input_dim=4, latent_dim=2, hidden_dim=32, 
                 regularization=None, reg_weight=1e-4):
        """
        VAE model with optional L1 or L2 regularization.

        Args:
            input_dim (int): Dimension of input features.
            latent_dim (int): Dimension of latent space.
            hidden_dim (int): Dimension of hidden layers.
            regularization (str, optional): Type of regularization ('l1', 'l2', or None).
            reg_weight (float, optional): Weight of the regularization term.
        """
        super(AE, self).__init__()

        self.regularization = regularization
        self.reg_weight = reg_weight

        # Encoder
        self.encoder = nn.Sequential(
            nn.Linear(input_dim-2, hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim, hidden_dim * 2),
            nn.BatchNorm1d(hidden_dim * 2),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.LeakyReLU(0.2),
        )

        # Mean and variance layers
        self.fc_mu = nn.Linear(hidden_dim, latent_dim)
        self.fc_var = nn.Linear(hidden_dim, latent_dim)

        # Decoder for input reconstruction
        self.decoder_input = nn.Sequential(
            nn.Linear(latent_dim, hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim, hidden_dim * 2),
            nn.BatchNorm1d(hidden_dim * 2),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim, input_dim-2)
        )

        #decoder for radius sum
        self.decoder_radius_sum = Decoder_loss(latent_dim, hidden_dim)

        # Decoder for SDF prediction
        self.decoder_sdf = nn.Sequential(
            nn.Linear(latent_dim+2, hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim, hidden_dim * 2),
            nn.BatchNorm1d(hidden_dim * 2),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim, 1)
        )

        self.init_weights()

    def init_weights(self):
        # Initialize encoder layers
        for layer in self.encoder:
            if isinstance(layer, nn.Linear):
                nn.init.xavier_uniform_(layer.weight)
                nn.init.zeros_(layer.bias)

        # Initialize mu and var layers
        nn.init.xavier_uniform_(self.fc_mu.weight)
        nn.init.zeros_(self.fc_mu.bias)
        nn.init.xavier_uniform_(self.fc_var.weight)
        nn.init.zeros_(self.fc_var.bias)

        # Initialize decoder_input layers
        for layer in self.decoder_input:
            if isinstance(layer, nn.Linear):
                nn.init.xavier_uniform_(layer.weight)
                nn.init.zeros_(layer.bias)

        # Initialize decoder_sdf layers
        for layer in self.decoder_sdf:
            if isinstance(layer, nn.Linear):
                nn.init.xavier_uniform_(layer.weight)
                nn.init.zeros_(layer.bias)

    def reparameterize(self, mu, log_var):
        std = torch.exp(0.5 * log_var)
        eps = torch.randn_like(std)
        return mu + eps * std

    def forward(self, x):
        # Encode
        query_points = x[:, :2]
        x_encoded = self.encoder(x[:, 2:])  # Only use x[2:] features
        mu = self.fc_mu(x_encoded)
        log_var = self.fc_var(x_encoded)

        # Reparameterization
        z = self.reparameterize(mu, log_var)

        # Decode
        x_reconstructed = self.decoder_input(z)
        sdf_decoder_input = torch.cat([z, query_points], dim=1)
        sdf_pred = self.decoder_sdf(sdf_decoder_input)

        return x_reconstructed, sdf_pred, z  # Return z for regularization
    
    def radius_sum(self, z):
        return self.decoder_radius_sum(z)
    
    def radius_sum_loss(self, radius_sum_pred, radius_sum_target):
        return F.mse_loss(radius_sum_pred, radius_sum_target, reduction='mean')

    def loss_function(self, x_reconstructed, x_original, sdf_pred, sdf_target, z, only_recon=False):
        """
        Calculate the loss function with optional L1 or L2 regularization.

        Args:
            x_reconstructed (Tensor): Reconstructed input.
            x_original (Tensor): Original input.
            sdf_pred (Tensor): Predicted SDF.
            sdf_target (Tensor): Target SDF.
            z (Tensor): Latent representations.

        Returns:
            total_loss (Tensor): Combined loss.
            recon_loss (Tensor): Reconstruction loss.
            sdf_loss (Tensor): SDF prediction loss.
            reg_loss (Tensor or 0): Regularization loss.
        """
        # Reconstruction loss for input features
        recon_loss = F.mse_loss(x_reconstructed, x_original[:, 2:], reduction='mean')

        # SDF prediction loss
        sdf_loss = F.mse_loss(sdf_pred, sdf_target.unsqueeze(1), reduction='mean')

        # Regularization loss
        if self.regularization == 'l1':
            reg_loss = torch.mean(torch.abs(z))
        elif self.regularization == 'l2':
            reg_loss = torch.mean(z.pow(2))
        else:
            reg_loss = 0


        if only_recon==False: 
            # Total loss
            # total_loss = recon_loss + sdf_loss + self.reg_weight * reg_loss
            total_loss = sdf_loss + self.reg_weight * reg_loss

        else:
            total_loss = recon_loss

        splitted_loss = {
            "total_loss": total_loss,
            "recon_loss": recon_loss,
            "sdf_loss": sdf_loss,
            "reg_loss": reg_loss
        }

        return total_loss, splitted_loss
    
    def sdf(self, z, query_points):
        z_repeated = z.repeat(query_points.shape[0], 1)

        # Decode
        sdf_decoder_input = torch.cat([z_repeated, query_points], dim=1)
        sdf_pred = self.decoder_sdf(sdf_decoder_input)

        return sdf_pred
    
class AE_DeepSDF(AE):
    def __init__(self, input_dim=4, latent_dim=2, hidden_dim=32, 
                 regularization=None, reg_weight=1e-4):
        super(AE_DeepSDF, self).__init__(input_dim, latent_dim, hidden_dim, 
                                        regularization, reg_weight)
        
        NetworkSpecs = {
            "latent_size" : latent_dim,
            "dims" : [ 512, 512, 512, 512, 512, 512, 512, 512 ],
            "dropout" : [0, 1, 2, 3, 4, 5, 6, 7],
            "dropout_prob" : 0.2,
            "norm_layers" : [0, 1, 2, 3, 4, 5, 6, 7],
            "latent_in" : [4],
            "xyz_in_all" : False,
            "use_tanh" : False,
            "latent_dropout" : False,
            "weight_norm" : False
        }
        
        self.decoder_sdf = Decoder(**NetworkSpecs)

class AE_explicit_radius(nn.Module):
    def __init__(self, input_dim=4, latent_dim=2, hidden_dim=32, rad_latent_dim=2,
                 rad_loss_weight=0.1, orthogonality_loss_weight=0.1,
                 regularization=None, reg_weight=1e-4):
        """
        VAE model with optional L1 or L2 regularization.

        Args:
            input_dim (int): Dimension of input features.
            latent_dim (int): Dimension of latent space.
            hidden_dim (int): Dimension of hidden layers.
            regularization (str, optional): Type of regularization ('l1', 'l2', or None).
            reg_weight (float, optional): Weight of the regularization term.
        """
        super(AE_explicit_radius, self).__init__()

        self.regularization = regularization
        self.reg_weight = reg_weight
        print(f"regularization: {regularization}, reg_weight: {reg_weight}")
        self.rad_latent_dim = rad_latent_dim
        self.rad_loss_weight = rad_loss_weight
        self.orthogonality_loss_weight = orthogonality_loss_weight

        # Encoder
        self.encoder = nn.Sequential(
            nn.Linear(input_dim-2, hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim, hidden_dim * 2),
            nn.BatchNorm1d(hidden_dim * 2),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim, latent_dim)
        )

        #decoder for radius sum
        self.decoder_radius_sum = nn.Linear(rad_latent_dim, 1)

        # Decoder for SDF prediction
        self.decoder_sdf = nn.Sequential(
            nn.Linear(latent_dim+2, hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim, hidden_dim * 2),
            nn.BatchNorm1d(hidden_dim * 2),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.LeakyReLU(0.2),
            nn.Linear(hidden_dim, 1)
        )

        self.init_weights()

    def init_weights(self):
        # Initialize encoder layers
        for layer in self.encoder:
            if isinstance(layer, nn.Linear):
                nn.init.xavier_uniform_(layer.weight)
                nn.init.zeros_(layer.bias)

        # Initialize decoder_sdf layers
        for layer in self.decoder_sdf:
            if isinstance(layer, nn.Linear):
                nn.init.xavier_uniform_(layer.weight)
                nn.init.zeros_(layer.bias)

        # Initialize decoder_radius_sum layers
        nn.init.xavier_uniform_(self.decoder_radius_sum.weight)
        nn.init.zeros_(self.decoder_radius_sum.bias)

    def forward(self, x):
        # Encode
        query_points = x[:, :2]
        z = self.encoder(x[:, 2:])  # Only use x[2:] features

        z_radius = z[:, :self.rad_latent_dim]
        radius_sum_pred = self.decoder_radius_sum(z_radius)

        sdf_decoder_input = torch.cat([z, query_points], dim=1)
        sdf_pred = self.decoder_sdf(sdf_decoder_input)

        return radius_sum_pred, sdf_pred, z  # Return z for regularization
    
    def radius_sum(self, z):
        return self.decoder_radius_sum(z)
    
    # def orthogonality_loss(self, z):
    #     return torch.norm(z.T @ z - torch.eye(z.shape[1]), p='fro')

    def orthogonality_loss(self, z):
        """
        to ensure that the explicit radius dimension remains disentangled from other latent features
        """
        z_radius = z[:, :self.rad_latent_dim]
        z_original = z[:, self.rad_latent_dim:]
        # return torch.norm(z_radius.T @ z_original, p='fro')

        Q_radius, R_radius = torch.linalg.qr(z_radius)
        Q_original, R_original = torch.linalg.qr(z_original)

        orthogonality_loss = -1 * torch.linalg.norm(Q_radius.T @ Q_original, ord=2)
        return orthogonality_loss
        

    def loss_function(self, radius_sum_pred, radius_sum_target, sdf_pred, sdf_target, z):
        """
        Calculate the loss function with optional L1 or L2 regularization.

        Args:
            x_reconstructed (Tensor): Reconstructed input.
            x_original (Tensor): Original input.
            sdf_pred (Tensor): Predicted SDF.
            sdf_target (Tensor): Target SDF.
            z (Tensor): Latent representations.

        Returns:
            total_loss (Tensor): Combined loss.
            radius_sum_loss (Tensor): Reconstruction loss.
            sdf_loss (Tensor): SDF prediction loss.
            reg_loss (Tensor or 0): Regularization loss.
        """
        # Reconstruction loss for input features
        radius_sum_loss = F.mse_loss(radius_sum_pred.flatten(), radius_sum_target.flatten(), reduction='mean')

        # SDF prediction loss
        sdf_loss = F.mse_loss(sdf_pred, sdf_target.unsqueeze(1), reduction='mean')

        # Orthogonality loss
        orthogonality_loss = self.orthogonality_loss(z)

        if not self.training:
            orthogonality_metric_value = orthogonality_metric(z, self.rad_latent_dim)
        else:
            orthogonality_metric_value = 0

        # Regularization loss
        if self.regularization == 'l1':
            reg_loss = torch.mean(torch.abs(z))
        elif self.regularization == 'l2':
            reg_loss = torch.mean(z.pow(2))
        else:
            reg_loss = 0

        total_loss = (
            sdf_loss 
            + self.rad_loss_weight * radius_sum_loss 
            + self.orthogonality_loss_weight * orthogonality_loss 
            + self.reg_weight * reg_loss
        )

        
        splitted_loss = {
            "total_loss": total_loss,
            "radius_sum_loss": radius_sum_loss,
            "sdf_loss": sdf_loss,
            "orthogonality_loss": orthogonality_loss,
            "reg_loss": reg_loss
        }

        if not self.training:
            splitted_loss["orthogonality_metric"] = orthogonality_metric_value

        return total_loss, splitted_loss
    
    def sdf(self, z, query_points):
        z_repeated = z.repeat(query_points.shape[0], 1)

        # Decode
        sdf_decoder_input = torch.cat([z_repeated, query_points], dim=1)
        sdf_pred = self.decoder_sdf(sdf_decoder_input)

        return sdf_pred
    
class AE_DeepSDF_explicit_radius(AE_explicit_radius):
    def __init__(self, input_dim=4, latent_dim=2, hidden_dim=32, rad_latent_dim=2, 
                 rad_loss_weight=0.1, orthogonality_loss_weight=0.1,
                 regularization=None, reg_weight=1e-4):
        super(AE_DeepSDF_explicit_radius, self).__init__(input_dim, latent_dim, hidden_dim, rad_latent_dim,
                                        rad_loss_weight, orthogonality_loss_weight, regularization, reg_weight)
        
        NetworkSpecs = {
            "latent_size" : latent_dim,
            "dims" : [ 512, 512, 512, 512, 512, 512, 512, 512 ],
            "dropout" : [0, 1, 2, 3, 4, 5, 6, 7],
            "dropout_prob" : 0.2,
            "norm_layers" : [0, 1, 2, 3, 4, 5, 6, 7],
            "latent_in" : [4],
            "xyz_in_all" : False,
            "use_tanh" : False,
            "latent_dropout" : False,
            "weight_norm" : False
        }
        
        self.decoder_sdf = Decoder(**NetworkSpecs)

class LitSdfAE(L.LightningModule):
    def __init__(self, vae_model, learning_rate=1e-4, reg_weight=1e-4, 
                 regularization=None, warmup_steps=1000, max_steps=10000):
        super().__init__()
        self.vae = vae_model
        self.learning_rate = learning_rate
        self.reg_weight = reg_weight
        self.regularization = regularization
        self.warmup_steps = warmup_steps
        self.max_steps = max_steps
        self.only_recon = False
        self.only_radius_sum = False
            
        self.save_hyperparameters(logger=False)

    def freeze_decoder_input(self, only_recon=False, only_radius_sum=False):
        for param in self.vae.parameters():
            param.requires_grad = False

        if only_radius_sum:
            for param in self.vae.decoder_radius_sum.parameters():
                param.requires_grad = True
                
        if only_recon:
            for param in self.vae.decoder_input.parameters():
                param.requires_grad = True

        self.only_recon = only_recon
        self.only_radius_sum = only_radius_sum

    def forward(self, x):
        return self.vae(x)

    def training_step(self, batch, batch_idx):
        x, sdf, radius_sum = batch
        radius_sum_pred, sdf_pred, z = self.vae(x)

        if self.only_radius_sum:
            radius_sum_loss = self.vae.radius_sum_loss(radius_sum_pred, radius_sum)
            total_loss = radius_sum_loss
            self.log('train_radius_sum_loss', radius_sum_loss, prog_bar=True)
        else:
            total_loss, splitted_loss = self.vae.loss_function(
                radius_sum_pred, radius_sum, sdf_pred, sdf, z
            )

            for key, value in splitted_loss.items():
                self.log(f'train_{key}', value, prog_bar=True)

        return total_loss

    def validation_step(self, batch, batch_idx, dataloader_idx=0):
        if dataloader_idx == 0:
            x, sdf, radius_sum = batch
            # print(f"x: {x.shape}, type: {x.dtype}")
            radius_sum_pred, sdf_pred, z = self.vae(x)

            if self.only_radius_sum:
                radius_sum_loss = self.vae.radius_sum_loss(radius_sum_pred, radius_sum)
                total_loss = radius_sum_loss
                self.log('val_radius_sum_loss', radius_sum_loss, prog_bar=True)
            else:
                total_loss, splitted_loss = self.vae.loss_function(
                    radius_sum_pred, radius_sum, sdf_pred, sdf, z
                )

                for key, value in splitted_loss.items():
                    self.log(f'val_{key}', value, prog_bar=True)
        else:
            total_loss = 0
            total_splitted_loss = {}
            xs, sdfs, points_s = batch
            total_mae = 0
            total_rmse = 0
            total_smoothness_pred = 0
            total_smoothness_diff = 0
            total_diff_smoothness = 0

            for i in range(len(points_s)):
                x = xs[i]
                sdf = sdfs[i]
                points = points_s[i]
                # radius_sum = radius_sums[i]

                # Attention: it is supposed that points are from a full grid
                points_per_side = int(np.sqrt(points.shape[0]))
                
                x_repeated = x.repeat(points.size(0), 1)
                x_combined = torch.cat((points, x_repeated), dim=1)
                radius_sum_pred, sdf_pred, z = self.vae(x_combined)

                sdf_diff = sdf_pred.squeeze() - sdf.squeeze()

                loss, splitted_loss = self.vae.loss_function(
                    radius_sum_pred, radius_sum_pred, sdf_pred, sdf, z
                )

                total_loss += loss

                for key, value in splitted_loss.items():
                    if key not in total_splitted_loss:
                        total_splitted_loss[key] = value
                    else:
                        total_splitted_loss[key] += value

                # Compute metrics
                mae, rmse = compute_closeness(sdf_pred, sdf)
                sdf_pred_reshaped = sdf_pred.reshape(points_per_side, points_per_side)
                sdf_target_reshaped = sdf.reshape(points_per_side, points_per_side)
                sdf_diff_reshaped = sdf_diff.reshape(points_per_side, points_per_side)
                smoothness_pred = finite_difference_smoothness_torch(sdf_pred_reshaped)
                smoothness_target = finite_difference_smoothness_torch(sdf_target_reshaped)
                smoothness_diff = finite_difference_smoothness_torch(sdf_diff_reshaped)
                diff_smoothness = smoothness_pred - smoothness_target

                total_mae += mae
                total_rmse += rmse
                total_smoothness_pred += smoothness_pred
                total_smoothness_diff += smoothness_diff
                total_diff_smoothness += diff_smoothness

            avg_splitted_loss = {key: value / len(batch) for key, value in total_splitted_loss.items()}
            avg_mae = total_mae / len(points_s)
            avg_rmse = total_rmse / len(points_s)
            avg_smoothness_pred = total_smoothness_pred / len(points_s)
            avg_smoothness_diff = total_smoothness_diff / len(points_s)
            avg_diff_smoothness = total_diff_smoothness / len(points_s)

            for key, value in avg_splitted_loss.items():
                self.log(f'val2_{key}', value, prog_bar=True)

            self.log('val2_mae', avg_mae, prog_bar=True)
            self.log('val2_rmse', avg_rmse, prog_bar=True)
            self.log('val2_smoothness_pred', avg_smoothness_pred, prog_bar=True)
            self.log('val2_smoothness_diff', avg_smoothness_diff, prog_bar=True)
            self.log('val2_diff_smoothness', avg_diff_smoothness, prog_bar=True)

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.parameters(), lr=self.learning_rate)

        warmup_lr_lambda = lambda step: (step + 1) / self.warmup_steps if step < self.warmup_steps else 1
        warmup_scheduler = LambdaLR(optimizer, lr_lambda=warmup_lr_lambda)

        cosine_scheduler = CosineAnnealingLR(optimizer, T_max=(self.max_steps - self.warmup_steps), eta_min=0)

        scheduler = {
            'scheduler': SequentialLR(
                optimizer, schedulers=[warmup_scheduler, cosine_scheduler], milestones=[self.warmup_steps]
            ),
            'interval': 'step',
            'frequency': 1
        }

        return {
            "optimizer": optimizer,
            'lr_scheduler': scheduler
        }