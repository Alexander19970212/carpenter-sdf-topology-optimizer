#imports
import gmsh 
import numpy as np
import sys
from tqdm import tqdm
import matplotlib.pyplot as plt
import matplotlib.tri as mtri

import torch

from scipy.sparse import linalg as sla
from scipy.sparse import coo_matrix
from scipy.spatial import distance_matrix
from scipy import sparse

from random import randint, randrange
import json

from NN_TopOpt.mesh_utils import LoadedMesh2D
import time

from NN_TopOpt.TopOpt import SIMP_basic
from NN_TopOpt.TopOpt import TopOptimizer2D
from NN_TopOpt.TopOpt import fit_ellipsoid


def if_nan(tensor):
    return torch.isnan(tensor).any()

def _points_to_grid(points, z_values, grid_size=40, half_side=1):
    grid = torch.zeros((1, grid_size, grid_size), dtype=torch.float32)
    
    # Normalize coordinates to [0, 1] range
    # points_norm = points.clone()
    # points_min = points.min(axis=0)
    # points_max = points.max(axis=0)
    points_min = torch.tensor(-half_side)
    points_max = torch.tensor(half_side)
    points_norm = (points - points_min) / (points_max - points_min)

    # print(points_norm.min(), points_norm.max())
    
    # Map to grid indices
    indices = (points_norm * (grid_size - 1)).type(torch.int32)
    
    # Create a sparse matrix to handle multiple points at same location
    sparse_grid = {}
    for idx, z in zip(indices, z_values):
        i, j = tuple(idx)
        if (i, j) in sparse_grid:
            sparse_grid[(i, j)].append(z)
        else:
            sparse_grid[(i, j)] = [z]
            
    # Fill grid with average z values
    for (i, j), z_values in sparse_grid.items():
        grid[0, i, j] = sum(z_values) / len(z_values)
        
    return grid


class FeatureMappingTopOpt:
    def __init__(self, args) -> None:

        top_opt_methods = {
            "FM_AE_DeepSDF": FeatureMappingDecSDF,
            "GaussianSplattingCompliance": GaussianSplattingCompliance,
            "CombinedMappingDecoderSDF": CombinedMappingDecoderSDF
        }

        self.max_iter = args["args"]["max_iter"]
        self.rs_loss_start_iter = args["args"]["rs_loss_start_iter"]
        self.volfrac = args["args"]["volfrac"]
        self.N_g_x = args["args"]["N_g_x"]
        self.N_g_y = args["args"]["N_g_y"]
        self.Emin = args["Emin"]
        self.Emax = args["Emax"]
        self.penal = args["penal"]
        self.shape_combination = args["args"]["shape_combination"]

        top_opt_method = top_opt_methods[args["args"]["top_opt_method"]]

        self.problem_config = args["problem_config"]

        self.Th = args["Th"]
        self.nme = self.Th.me.shape[0]
        self.x = self.volfrac * np.ones(self.Th.me.shape[0],dtype=float)
        self.x_old = self.x.copy()
        self.coords = torch.tensor(self.Th.centroids)
        self.volumes = torch.tensor(self.Th.areas)
        self.H = torch.from_numpy(self.x)

        self.dc = np.ones(self.Th.me.shape[0])

        # self.gaussian_core = GaussianMixCompliance(dist_means, dist_stdves, self.coords, self.Emin, self.Emax, self.penal)        
        # self.gaussian_core = GaussianSplattingCompliance(dist_means, self.coords, self.Emin, self.Emax, self.penal, num_gaussian, args) 
        self.gaussian_core = top_opt_method(self.coords, self.volumes, args)
        # self.gaussian_core.load_state_dict(torch.load('test_problems/checkpoints/gaussian_core_iter160.pt'))

        self.stop_flag = False
        self.obj = 0

        self.global_i = 0
        self.meta = {'x': self.x.copy().tolist(), 'dc': self.dc.copy().tolist(), 'stop_flag': self.stop_flag}

        ## Optimizer
        # TODO: check for different vars
        self.optim = torch.optim.Adam([
            {'params': self.gaussian_core.W_scale, 'lr': 5e-2},
            {'params': self.gaussian_core.W_shape_var, 'lr': 1e-1},
            {'params': self.gaussian_core.W_rotation, 'lr': 5e-2},
            {'params': self.gaussian_core.W_offsets, 'lr': 1e-1}
        ], maximize=False, eps=1e-8)

    def parameter_opt_step(self, ce):
        # for id in range(2):
        #     _ = self.gaussian_core.get_x(self.global_i)
        self.optim.zero_grad()
        loss, splitted_loss = self.gaussian_core(
            torch.tensor(ce), self.global_i
        )
        # Check for NaN before backward pass
        if torch.isnan(loss):
            print("Warning: NaN detected in loss")
        print(f"Loss: {loss.item()}")
        loss.backward(retain_graph=True)  # Add retain_graph=True to keep computation graph

        # Check for NaN gradients
        for name, param in self.gaussian_core.named_parameters():
            if param.grad is not None and torch.isnan(param.grad).any():
                print(f"NaN gradient detected in {name}")
                param.grad = torch.where(torch.isnan(param.grad), 
                                    torch.zeros_like(param.grad), 
                                    param.grad)
                
        self.gaussian_core.prepare_grads(self.global_i)

        # TODO: check for different vars
        # torch.nn.utils.clip_grad_norm_(self.gaussian_core.parameters(), max_norm=0.02)

        # Compute gradient norms for different parameters
        grad_norms = {}
        for name, param in self.gaussian_core.named_parameters():
            if param.grad is not None:
                grad_norm = param.grad.norm()
                grad_norms[name] = grad_norm

        # print("grad_norms: ", grad_norms)
        # Combine splitted_loss with grad_norms
        combined_metrics = {**splitted_loss, **grad_norms}
        # print("combined_loss: ", combined_loss)

        self.optim.step()

        if self.shape_combination:
            self.gaussian_core.update_shape_combination(self.global_i)

        return loss, combined_metrics
    
    def plot_H_gradients(self, ce):
        """Plot gradients of H field as a 2D heatmap"""
        # Get H tensor and compute gradients
        self.optim.zero_grad()
        loss, splitted_loss = self.gaussian_core(
            torch.tensor(ce), self.global_i
        )

        # loss.backward()
    
        # Get gradients using autograd.grad instead of .grad attribute
        if self.global_i >= self.rs_loss_start_iter+2:
            print("rs_loss: ", splitted_loss["rs_loss"])
            grad_H = torch.autograd.grad(splitted_loss["rs_loss"], self.gaussian_core.H_inverted, create_graph=False, retain_graph=True)[0]
        else:
            grad_H = torch.autograd.grad(splitted_loss["compliance"], self.gaussian_core.H, create_graph=False, retain_graph=True)[0]

        # Convert to numpy and reshape to 2D grid
        grad_magnitude = torch.sqrt(grad_H**2).numpy()
        
        # Create figure
        fig = plt.figure(figsize=(12,4))
        
        # Plot H field with gradient magnitude overlay
        contour_h = plt.tricontourf(self.coords[:,0], self.coords[:,1],
                                   self.gaussian_core.H.detach().numpy(), cmap='viridis', 
                                   levels=50)
        plt.colorbar(contour_h, label='H Value')
        
        contour_grad = plt.tricontourf(self.coords[:,0], self.coords[:,1], 
                                      grad_magnitude, cmap='magma', alpha=0.3,
                                      levels=20) 
        plt.colorbar(contour_grad, label='Gradient Magnitude')
        
        plt.title('H Field with Gradient Magnitude Overlay')
        plt.xlabel('X')
        plt.ylabel('Y')
        
        # Set axis limits based on coordinate bounds
        x_min, x_max = self.coords[:,0].min(), self.coords[:,0].max()
        y_min, y_max = self.coords[:,1].min(), self.coords[:,1].max()
        plt.xlim(x_min, x_max)
        plt.ylim(y_min, y_max)
        
        plt.axis('equal')
        # plt.tight_layout()
        
        plt.show()

    def get_x(self, args):
        
        if self.global_i != 0:
            ce = args["ce"]
            self.plot_H_gradients(ce)
            obj, splitted_loss = self.parameter_opt_step(ce)

        # if self.global_i == 160:
            # Save model parameters at iteration 10
            # torch.save(self.gaussian_core.state_dict(), f'test_problems/checkpoints/gaussian_core_iter160.pt')

        self.xold = self.x.copy()
        self.x = np.ones_like(self.x_old)*self.Emax
   
        self.H = self.gaussian_core.get_x(self.global_i)
        self.x = self.H.detach().numpy()
        
        change=np.linalg.norm(self.x.reshape(self.nme,1)-self.xold.reshape(self.nme,1),np.inf).item()


        if self.global_i+1 == self.max_iter:
            self.stop_flag = True

        if self.global_i != 0:

            self.meta = {'x': self.x.copy().tolist(),
                        'ce': ce.copy().tolist(),
                        # 'obj_opt': obj.item(),
                        # 'obj': obj_real.item(),
                        # 'volfrac_loss_pre': volfrac_loss_pre.item(),
                        # 'gaussian_overlap': gaussian_overlap.item(),
                        # 'ff_loss': ff_loss.item(),
                        # 'rs_loss': rs_loss.item(),
                        # 'compliance': compliance.item(),
                        'dc': self.dc.copy().tolist(),
                        'change': change,
                        'stop_flag': self.stop_flag}
            
            for key, value in splitted_loss.items():
                self.meta[key] = value.item()
            
            print(self.global_i, obj.item(), change)

        self.global_i += 1
        
        return self.x.copy()
    
    def update_problem_config(self, new_problem_name):
        new_parameters = self.gaussian_core.get_final_x()

        self.problem_config.update(new_parameters)

        with open('test_problems/problems.json', 'r') as fp:
            problem_list = json.load(fp)

        problem_list[new_problem_name] = self.problem_config

        with open('test_problems/problems.json', 'w') as f:
            json.dump(problem_list, f)

from models.sdf_models import AE_DeepSDF
from models.rs_loss_models import RSLossPredictorResNet50

class FeatureMappingDecSDF(torch.nn.Module):
    def __init__(self, coords, volumes, args):
        super().__init__()

        self.compliance_w = args["args"]["compliance_w"]
        self.volfrac_w = args["args"]["volfrac_w"]
        self.gaussian_overlap_w = args["args"]["gaussian_overlap_w"]
        self.gaussian_overlap_scale = args["args"]["gaussian_overlap_scale"]
        self.ff_loss_w = args["args"]["ff_loss_w"]
        self.smooth_k = args["args"]["smooth_k"]
        self.saved_model_name = args["args"]["saved_model_name"]
        # self.decoder_radius_sum_name = args["args"]["decoder_radius_sum_name"]
        num_samples = args["args"]["N_g"]

        problem_config = args["problem_config"]

        self.coords = coords.to(torch.float32)


        init_offsets = torch.tensor(problem_config["offsets"]).to(torch.float32)
        init_scales = torch.tensor(problem_config["scale"])*3
        init_sigmas_ratios = torch.tensor(problem_config["sigmas_ratio"]).flatten().to(torch.float32)
        init_rotations = torch.tensor(problem_config["rotation"])

        self.Emin = args["Emin"]
        self.Emax = args["Emax"]
        self.penal = args["penal"]
        self.volfrac = args["args"]["volfrac"]

        # load stats for latent space
        latent_goal = args["args"]["latent_goal"]
        data = np.load(f"model_weights/{self.saved_model_name}_{latent_goal}_stats.npz")
        self.latent_goal_center = torch.tensor(data['center'], dtype=torch.float32)
        self.latent_goal_cov_inv = torch.tensor(data['class_cov_inv'], dtype=torch.float32)
        self.latent_mins = torch.tensor(data['latent_mins'], dtype=torch.float32) * 1.2
        self.latent_maxs = torch.tensor(data['latent_maxs'], dtype=torch.float32) * 1.2
        self.latent_dim = self.latent_mins.shape[0]

        # RS Loss
        if args["args"]["rs_loss"]:
            self.rs_loss = True
            self.rs_loss_w = args["args"]["rs_loss_w"]
            self.rs_loss_e = args["args"]["rs_loss_e"]
            self.rs_loss_convex_threshold = args["args"]["rs_loss_convex_threshold"]
            self.rs_loss_plato_threshold = args["args"]["rs_loss_plato_threshold"]
            self.rs_loss_start_iter = args["args"]["rs_loss_start_iter"]
            self.rs_loss_small_cos_dist_threshold = args["args"]["rs_loss_small_cos_dist_threshold"]

            print("computing nearest neighbors ...")

            # self.rs_loss_neighbors_indices, self.rs_loss_inverted_matrices = find_nearest_neighbors(self.coords[:, 0], self.coords[:, 1], self.rs_loss_e)
            self.rs_loss_vector = None

        # self.model = VAE(input_dim=10, latent_dim=3, hidden_dim=128)
        # self.model = AE_DeepSDF(
        #     input_dim=17, 
        #     latent_dim=self.latent_dim, 
        #     hidden_dim=128, 
        #     regularization='l2',   # Use 'l1', 'l2', or None
        #     reg_weight=1e-3        # Adjust the weight as needed
        # )

        self.model = AE_DeepSDF_explicit_radius(
            input_dim=17, 
            latent_dim=self.latent_dim, 
            hidden_dim=128, 
            rad_latent_dim=2,
            rad_loss_weight=0.1,
            regularization='l2',   # Use 'l1', 'l2', or None
            reg_weight=1e-3        # Adjust the weight as needed
        )
        self.model.load_state_dict(torch.load(f"model_weights/{self.saved_model_name}.pt"), strict=False)
        self.model.eval()

        # self.decoder_radius_sum = Decoder_loss(latent_dim=self.latent_dim, hidden_dim=128)
        # self.decoder_radius_sum.load_state_dict(torch.load(f"model_weights/{self.decoder_radius_sum_name}.pt"))
        # self.decoder_radius_sum.eval()

        # create model for rs_loss
        # checkpoint_path = 'model_weights/rs_loss_conv_combined.ckpt'
        checkpoint_path = 'model_weights/rs_loss_conv.ckpt'
        self.rs_loss_predictor = RSLossPredictorResNet50.load_from_checkpoint(checkpoint_path)
        self.rs_loss_predictor.eval()

        # Create input vector for encoder
        encoder_input = torch.zeros(init_offsets.shape[0], 17)
        encoder_input[:, 3] = init_sigmas_ratios  # Set scale values at index 1

        # get latent vectors
        _, _, z = self.model(encoder_input)

        self.sigma_min = 0.002
        self.sigma_max = 0.1

        self.scale_sigma = torch.tensor(0.06)/4
        self.scale_max = self.scale_sigma*10
        self.scale_min = self.scale_sigma*4

        self.rotation_min = -torch.pi/2
        self.rotation_max = torch.pi/2

        # self.shape_var_mins = torch.tensor([-2.7, -1.8, -2.9, -0.3, -3.3, -1.5])
        # self.shape_var_maxs = torch.tensor([2.2, 1.6, 3.7, 0.3, 2.7, 1.6])

        self.shape_var_mins = self.latent_mins
        self.shape_var_maxs = self.latent_maxs
        z = torch.clamp(z, self.shape_var_mins, self.shape_var_maxs)
        init_scales = torch.clamp(init_scales, self.scale_min, self.scale_max)

        # print("shape_var_mins: ", self.shape_var_mins)
        # print("shape_var_maxs: ", self.shape_var_maxs)
        # print("z: ", z)


        shape_variables = torch.zeros((num_samples, z.shape[1])).to(torch.float32)
        offsets = torch.zeros((num_samples, 2)).to(torch.float32)
        scales = torch.ones(num_samples).to(torch.float32)
        rotations = torch.zeros(num_samples).to(torch.float32)


        init_num_samples = z.shape[0]

        offsets[:init_num_samples] = init_offsets
        scales[:init_num_samples] = init_scales
        rotations[:init_num_samples] = init_rotations
        shape_variables[:init_num_samples] = z

        x_min = coords[:, 0].min()
        x_max = coords[:, 0].max()
        y_min = coords[:, 1].min()
        y_max = coords[:, 1].max()

        self.coord_max = torch.tensor([x_max, y_max]).to(torch.float32)
        self.coord_min = torch.tensor([x_min, y_min]).to(torch.float32)

        offsets_values = torch.logit((offsets - self.coord_min)/(self.coord_max - self.coord_min)).type(torch.float32)
        scale_values = torch.logit((scales - self.scale_min)/(self.scale_max - self.scale_min)).type(torch.float32)
        rotation_values = torch.logit((rotations - self.rotation_min)/(self.rotation_max - self.rotation_min)).type(torch.float32)
        
        shape_var_values = torch.logit((shape_variables - self.shape_var_mins)/(self.shape_var_maxs - self.shape_var_mins)).type(torch.float32)

        self.W_scale = torch.nn.Parameter(scale_values)
        self.W_shape_var = torch.nn.Parameter(shape_var_values)
        self.W_rotation = torch.nn.Parameter(rotation_values)
        self.W_offsets = torch.nn.Parameter(offsets_values)

        left_over_size = num_samples - init_num_samples
        self.current_marker = init_num_samples

        self.persistent_mask = torch.cat([torch.ones(init_num_samples, dtype=bool),torch.zeros(left_over_size, dtype=bool)], dim=0)

    def get_x(self, global_i):

        W_scale = self.W_scale[self.persistent_mask]
        W_shape_var = self.W_shape_var[self.persistent_mask] 
        W_rotation = self.W_rotation[self.persistent_mask]
        W_offsets = self.W_offsets[self.persistent_mask]
        batch_size = W_scale.shape[0]

        base_scale = (self.scale_max - self.scale_min)*torch.sigmoid(W_scale) + self.scale_min

        shape_var = (self.shape_var_maxs - self.shape_var_mins)*torch.sigmoid(W_shape_var) + self.shape_var_mins    
        rotation = self.rotation_min + (self.rotation_max - self.rotation_min)*torch.sigmoid(W_rotation).view(batch_size)
        offsets = self.coord_min + (self.coord_max - self.coord_min)*torch.sigmoid(W_offsets)

        # print("rotation: ", rotation.shape)
        # print("offsets: ", offsets.shape)
        print("base_scale: ", base_scale.shape)
        # print("coords: ", self.coords.shape)

        cos_rot = torch.cos(rotation)
        sin_rot = torch.sin(rotation)

        # rotation matrix
        R = torch.stack([
            torch.stack([cos_rot, -sin_rot], dim=-1),
            torch.stack([sin_rot, cos_rot], dim=-1)
        ], dim=-2)

        # Compute inverse covariance
        coords = self.coords.unsqueeze(0).expand(batch_size, -1, -1, -1)
        offsets = offsets[:, None, None, :]
        xy = coords - offsets

        scaled_xy = xy /((base_scale[:, None, None, None])+1e-8)
        scaled_xy_shifted = xy/(self.gaussian_overlap_scale*base_scale[:, None, None, None]+1e-8)


        print("scaled_xy: ", scaled_xy.shape)
        # print("R: ", R.shape)
        # Apply rotation to coordinates
        rotated_scaled_xy = torch.einsum('bij,bxyj->bxyi', R, scaled_xy)
        rotated_scaled_xy_shifted = torch.einsum('bij,bxyj->bxyi', R, scaled_xy_shifted)
        
        rotated_scaled_xy = rotated_scaled_xy.view(batch_size, -1, 2)
        rotated_scaled_xy_shifted = rotated_scaled_xy_shifted.view(batch_size, -1, 2)

        # print("rotated_scaled_xy: ", rotated_scaled_xy.shape)
        
        # Calculate z using pre_inv_covariance (no rotation)
        # rot_scaled_xy = torch.einsum('bxyi,bij,bxyj->bxy', rotated_xy, -0.5 * pre_inv_covariance, rotated_xy)
        kernel = torch.zeros(self.coords.shape[0], batch_size)#.double()
        kernel_shifted = torch.zeros(self.coords.shape[0], batch_size)#.double()

        for i in range(batch_size):
            mask = (rotated_scaled_xy[i].abs().max(dim=-1)[0] < 1)
            mask_shifted = (rotated_scaled_xy_shifted[i].abs().max(dim=-1)[0] < 1)
            sdfs = self.model.sdf(shape_var[i], rotated_scaled_xy[i, mask, :])
            sdfs_shifted = self.model.sdf(shape_var[i], rotated_scaled_xy_shifted[i, mask_shifted, :])
            kernel[mask, i] = sdfs.squeeze()#.double()
            kernel_shifted[mask_shifted, i] = sdfs_shifted.squeeze()#.double()

        kernel_sum = kernel.sum(dim=1)+1e-8

        self.H = (1 - self.Emin)*torch.sigmoid(-self.smooth_k*(kernel_sum - 0.5)) + self.Emin
        # self.H_splitted = torch.sigmoid(0.1*self.smooth_k*(kernel_shifted - 0.5))
        self.H_splitted = kernel_shifted
        self.H_splitted_sum = self.H_splitted.sum(dim=1)
        self.H_inverted = kernel_sum*0.5

        grid_batch = []

        if global_i >= self.rs_loss_start_iter:
            for i in range(batch_size):
                half_side = 1.5
                mask = (rotated_scaled_xy[i].abs().max(dim=-1)[0] < half_side)
                rotated_scaled_xy_rs = rotated_scaled_xy[i, mask, :]

                mapped_grid = _points_to_grid(rotated_scaled_xy_rs, self.H_inverted[mask], half_side=half_side)
                grid_batch.append(mapped_grid)
                # rs_loss_pred = self.rs_loss_predictor(rotated_scaled_xy_rs)

                # Reshape grid for visualization
                # grid_size = 40
                # grid_2d = mapped_grid.reshape(grid_size, grid_size).detach().numpy()
                # grid_2d = mapped_grid[0].detach().numpy()
                
                # plt.figure(figsize=(6,6))
                # plt.imshow(grid_2d, cmap='viridis', origin='lower')
                # plt.colorbar()
                # plt.title('Mapped Grid')
                # plt.axis('equal')
                # plt.show()

                # print("rs_loss_pred: ", rs_loss_pred.shape)

            rs_loss_grid = torch.stack(grid_batch)
            self.rs_loss_vector = self.rs_loss_predictor(rs_loss_grid)

            print("rs_loss: ", self.rs_loss_vector.min(), self.rs_loss_vector.max())
        
        # H_splitted_sum_clipped = self.H_splitted_sum.clone()
        # H_splitted_sum_clipped[H_splitted_sum_clipped>0.8] = 0
        # H_splitted_sum_clipped[H_splitted_sum_clipped<0.2] = 0 

        # Compute entropy for H_splitted_sum
        # Add small epsilon to avoid log(0)
        # epsilon = 1e-8
        # p = H_splitted_sum_clipped + epsilon
        # p = p / (p.sum() + epsilon)  # Normalize to get probability distribution
        # entropy = -torch.sum(p * torch.log(p))

        # Create entropy plot
        # plt.figure(figsize=(12, 4))
        # plt.subplot(121)
        # plt.tricontourf(self.coords[:, 0], self.coords[:, 1], p.detach().numpy(), cmap='viridis', levels=20)
        # plt.colorbar()
        # plt.title(f'Normalized H_splitted_sum (Entropy: {entropy.item():.4f})')
        # plt.axis('equal')

        # Create scatter plot of H_splitted_sum using coords
        # plt.figure(figsize=(12, 4))
        # plt.tricontourf(self.coords[:, 0], self.coords[:, 1], self.H_splitted_sum.detach().numpy(), cmap='viridis', levels=20)
        # plt.colorbar()
        # plt.title('H_splitted_sum')
        # plt.axis('equal')

        self.H_splitted_sum_clipped = torch.nn.functional.relu(
            self.H_splitted_sum - 1
        )

        return self.H

    def prepare_grads(self):
        self.W_scale.grad.data[~self.persistent_mask] = 0.0
        self.W_shape_var.grad.data[~self.persistent_mask] = 0.0
        self.W_rotation.grad.data[~self.persistent_mask] = 0.0
        self.W_offsets.grad.data[~self.persistent_mask] = 0.0

    def compute_ff_loss1(self): # form factor loss
        # get reconstruction code from decoder_input
        W_shape_var = self.W_shape_var[self.persistent_mask]
        shape_var = (self.shape_var_maxs - self.shape_var_mins)*torch.sigmoid(W_shape_var) + self.shape_var_mins 
        shape_code = self.model.decoder_input(shape_var)

        q0 = shape_code[:, 0]

        ff_loss = torch.nn.functional.relu(0.5 - q0)
        
        return ff_loss.sum()
    
    def compute_ff_loss2(self): # form factor loss
        
        latent_center = self.latent_goal_center
        latent_cov_inv = self.latent_goal_cov_inv

        W_shape_var = self.W_shape_var[self.persistent_mask]
        shape_var = (self.shape_var_maxs - self.shape_var_mins)*torch.sigmoid(W_shape_var) + self.shape_var_mins 
        
        dist = torch.norm(shape_var - latent_center, dim=1) # TODO: check if this is correct
        maha_dist_sq = torch.sum((shape_var - latent_center) @ latent_cov_inv @ (shape_var - latent_center).T, dim=1)
        # print("dist:", dist)
        # ff_loss = torch.nn.functional.leaky_relu(dist - 1.5, negative_slope=0.1)
        # ff_loss = torch.nn.functional.relu(maha_dist_sq - 1000.0)
        ff_loss = torch.nn.functional.relu(dist - 0.8)

        print("ff_loss: ", ff_loss.min(), ff_loss.max())
        print("maha_dist_sq: ", maha_dist_sq.min(), maha_dist_sq.max())
        print("dist: ", dist.shape, dist.min(), dist.max())

        return ff_loss.sum()
    
    def compute_ff_loss(self, global_i): # form factor loss
        
        if global_i > 5:

            W_shape_var = self.W_shape_var[self.persistent_mask]
            shape_var = (self.shape_var_maxs - self.shape_var_mins)*torch.sigmoid(W_shape_var) + self.shape_var_mins 
            
            radius_sum = self.model.radius_sum(shape_var[:, :2])

            print("radius_sum: ", radius_sum.min(), radius_sum.max(), radius_sum.sum())

            return radius_sum.sum()

        #######################################################################

        else:
            latent_center = self.latent_goal_center
            latent_cov_inv = self.latent_goal_cov_inv
            W_shape_var = self.W_shape_var[self.persistent_mask]
            shape_var = (self.shape_var_maxs - self.shape_var_mins)*torch.sigmoid(W_shape_var) + self.shape_var_mins 
            
            dist = torch.norm(shape_var - latent_center, dim=1) # TODO: check if this is correct
            maha_dist_sq = torch.sum((shape_var - latent_center) @ latent_cov_inv @ (shape_var - latent_center).T, dim=1)
            # print("dist:", dist)
            # ff_loss = torch.nn.functional.leaky_relu(dist - 1.5, negative_slope=0.1)
            # ff_loss = torch.nn.functional.relu(maha_dist_sq - 1000.0)
            ff_loss = torch.nn.functional.relu(dist - 0.8)

            return ff_loss.sum()
    
    def forward(self, ce, global_i):

        ff_loss = self.compute_ff_loss(global_i)

        # Add numerical stability to compliance calculation
        H_vec = torch.clamp(self.H, min=self.Emin, max=self.Emax)**self.penal
        compliance = torch.dot(H_vec, ce.float())

        volfrac_goal = self.volfrac - 0.2*max(0, min(1, (global_i-20)/20))
        
        # Safeguard the loss calculations
        volfrac_loss_pre = torch.nn.functional.relu(
            self.H.mean() - volfrac_goal
        )
        
        gaussian_overlap = self.H_splitted_sum_clipped.mean()

        print("volume: ", self.H.mean())
        print("compliance: ", compliance)
        print("gaussian_overlap: ", gaussian_overlap)
        print("ff_loss: ", ff_loss)

        # check the sign
        # Start of Selection
        obj_ce = (
            -compliance * self.compliance_w
            + ff_loss * self.ff_loss_w
            + volfrac_loss_pre * self.volfrac_w
            + gaussian_overlap * self.gaussian_overlap_w
        )
        obj_real = (
            compliance * self.compliance_w
            + ff_loss * self.ff_loss_w
            + volfrac_loss_pre * self.volfrac_w
            + gaussian_overlap * self.gaussian_overlap_w
        )

        if self.rs_loss and global_i >= self.rs_loss_start_iter+1:
            # print("indices: ", len(self.rs_loss_neighbors_indices), self.rs_loss_neighbors_indices[0].shape)
            # rs_loss, _ = compute_rs_loss(self.coords[:, 0], self.coords[:, 1], self.H_splitted_sum,
            #                           self.rs_loss_neighbors_indices, self.rs_loss_inverted_matrices, 
            #                           self.rs_loss_e, self.rs_loss_convex_threshold,
            #                           self.rs_loss_plato_threshold, self.rs_loss_small_cos_dist_threshold,
            #                           visualize=True)
            rs_loss = self.rs_loss_vector.mean()
            rs_loss_weighted = rs_loss * self.rs_loss_w

            obj_real += rs_loss_weighted
            obj_ce += rs_loss_weighted

        else:

            rs_loss = torch.tensor(0.0)

        splitted_loss = {
            "gaussian_overlap": gaussian_overlap,
            "volfrac_loss_pre": volfrac_loss_pre,
            "compliance": compliance,
            "ff_loss": ff_loss,
            "rs_loss": rs_loss,
            "obj_ce": obj_ce,
            "obj_real": obj_real,
        }

        return obj_ce, splitted_loss


class GaussianSplattingCompliance(torch.nn.Module):
    def __init__(self, coords, volumes, args):
        super().__init__()

        # self.sigma_min = 0.002
        # self.sigma_max = 0.1
        # self.det_min = 3e-06
        # self.det_max = 5.5e-06
        num_samples = args["args"]["N_g"]
        dist_means = torch.zeros((num_samples, 2))

        # initialize the gaussian centers according to the grid
        center_x = 0.3
        center_y = 0.1
        x = torch.linspace(0.05, 0.55, args["args"]["N_g_x"])
        y = torch.linspace(0.05, 0.15, args["args"]["N_g_y"])
        # print("x: ", x)
        # print("y: ", y)

        x = x + (x.mean() - center_x)
        y = y - (y.mean() - center_y)
        # print("x: ", x)
        # print("y: ", y)
        grid_x, grid_y = torch.meshgrid(x, y, indexing='ij')
        dist_means_init = torch.stack([grid_x.flatten(), grid_y.flatten()], dim=1)[:args["args"]["N_g_x"]*args["args"]["N_g_y"]]

        init_num_samples = dist_means_init.shape[0]

        dist_means[:init_num_samples] = dist_means_init

        # scale_x = 1/coords[:, 0].max()
        # self.width_ratio = coords[:, 1].max()/coords[:, 0].max()
        # self.scaler = torch.tensor([1, self.width_ratio])
        self.scale_sigma = torch.tensor(0.03)
        self.scale_max = self.scale_sigma*2
        self.scale_min = self.scale_sigma*0.9

        self.rotation_min = -torch.pi/2
        self.rotation_max = torch.pi/2

        x_min = coords[:, 0].min()
        x_max = coords[:, 0].max()
        y_min = coords[:, 1].min()
        y_max = coords[:, 1].max()

        self.coord_max = torch.tensor([x_max, y_max])
        self.coord_min = torch.tensor([x_min, y_min])

        print("Scale_sigma_init: ", self.scale_sigma)

        self.sigmas_ratio_max = 2.5
        self.sigmas_ratio_min = 0.5

        self.coords = coords

        offsets_values = torch.logit(
            (dist_means - self.coord_min) / (self.coord_max - self.coord_min)
        ).type(torch.DoubleTensor)
        
        scale_values = torch.logit(
            (torch.ones((num_samples, 1)) * self.scale_sigma - self.scale_min) / 
            (self.scale_max - self.scale_min)
        ).type(torch.DoubleTensor)
        
        sigmas_ratio_values = torch.logit(
            (torch.ones((num_samples, 1)) - self.sigmas_ratio_min) / 
            (self.sigmas_ratio_max - self.sigmas_ratio_min)
        ).type(torch.DoubleTensor)
        
        rotation_values = torch.logit(
            (torch.zeros((num_samples, 1)) - self.rotation_min) / 
            (self.rotation_max - self.rotation_min)
        ).type(torch.DoubleTensor)

        self.W_scale = torch.nn.Parameter(scale_values)
        self.W_shape_var = torch.nn.Parameter(sigmas_ratio_values)
        self.W_rotation = torch.nn.Parameter(rotation_values)
        self.W_offsets = torch.nn.Parameter(offsets_values)

        left_over_size = num_samples - init_num_samples
        self.current_marker = init_num_samples

        self.persistent_mask = torch.cat([torch.ones(init_num_samples, dtype=bool),torch.zeros(left_over_size, dtype=bool)], dim=0)
        self.new_gaussians_mask = torch.zeros_like(self.persistent_mask, dtype=bool)

        self.Emin = args["Emin"]
        self.Emax = args["Emax"]
        self.penal = args["penal"]
        self.volfrac = args["args"]["volfrac"]

        self.compliance_w = args["args"]["compliance_w"]
        self.volfrac_w = args["args"]["volfrac_w"]
        self.gaussian_overlap_w = args["args"]["gaussian_overlap_w"]
        self.smooth_k = args["args"]["smooth_k"]

        self.merging_marker = args["args"]["merging_marker"]
        self.merging_adaptation_length = args["args"]["merging_adaptation_length"]

        self.points_inside_elements = {}

    def get_x(self, global_i):

        # TODO: rewrite becasu sclaing is used twice

        W_scale = self.W_scale[self.persistent_mask]
        W_shape_var = self.W_shape_var[self.persistent_mask] 
        W_rotation = self.W_rotation[self.persistent_mask]
        W_offsets = self.W_offsets[self.persistent_mask]
        batch_size = W_scale.shape[0]

        base_scale = (self.scale_max - self.scale_min)*torch.sigmoid(W_scale) + self.scale_min
        base_scale = base_scale.squeeze()
        sigmas_ratio = (self.sigmas_ratio_max - self.sigmas_ratio_min)*torch.sigmoid(W_shape_var) + self.sigmas_ratio_min    
        sigmas = torch.cat([torch.ones(batch_size, 1), torch.ones(batch_size, 1) * sigmas_ratio], dim=1)
        rotation = self.rotation_min + (self.rotation_max - self.rotation_min)*torch.sigmoid(W_rotation).view(batch_size)
        offsets = self.coord_min + (self.coord_max - self.coord_min)*torch.sigmoid(W_offsets)

        cos_rot = torch.cos(rotation)
        sin_rot = torch.sin(rotation)

        # rotation matrix
        R = torch.stack([
            torch.stack([cos_rot, -sin_rot], dim=-1),
            torch.stack([sin_rot, cos_rot], dim=-1)
        ], dim=-2)

        covariance_diag = sigmas**2
        pre_inv_covariance_diag = 1/(covariance_diag + 1e-6)

        pre_inv_covariance = torch.diag_embed(pre_inv_covariance_diag)
        # pre_inv_covariance = pre_inv_covariance

        # Compute inverse covariance
        coords = self.coords.unsqueeze(0).expand(batch_size, -1, -1, -1)
        offsets = offsets[:, None, None, :]
        xy = coords - offsets

        # TODO: check if this is correct (not multiplying by base_scale)
        xy = xy /((base_scale[:, None, None, None])+1e-8)

        # Apply rotation to coordinates
        rotated_xy = torch.einsum('bij,bxyj->bxyi', R, xy)
        
        # Calculate z using pre_inv_covariance (no rotation)
        z = torch.einsum('bxyi,bij,bxyj->bxy', rotated_xy, pre_inv_covariance, rotated_xy)
        z = torch.sqrt(z)

        print("z: ", z.min(), z.max())
        
        # kernel = torch.exp(20*z)
        kernel = 1/(1 + torch.exp(10*(z-1)))
        kernel = torch.squeeze(kernel)

        print("kernel: ", kernel.min(), kernel.max())
             
        kernel_sum = kernel.sum(dim=0)+1e-8

        if global_i == self.merging_marker-1:
            print("merging preparation")
            self.compute_merging_pairs(kernel)

        self.H = (1 - self.Emin)*torch.sigmoid(-self.smooth_k*(kernel_sum - 0.5)) + self.Emin

        # TODO: check if this is correct (splitted sum)
        self.H_splitted = (1 - self.Emin)*torch.sigmoid(1.8*self.smooth_k*(kernel - 0.1)) + self.Emin
        self.H_splitted_sum = self.H_splitted.sum(dim=0)
        # self.H_offset = (1 - self.Emin)*torch.sigmoid(-4*torch.log(kernel_sum*1.1)) + self.Emin

        return self.H

    def prepare_grads(self, global_i):
        # print("persistent_mask: ", self.persistent_mask)
        if (global_i > self.merging_marker and 
            global_i < self.merging_marker + self.merging_adaptation_length and 
            self.new_gaussians_mask.any()):
            evolving_mask = self.new_gaussians_mask
        else:
            evolving_mask = self.persistent_mask

        print("evolving_mask: ", evolving_mask)

        self.W_scale.grad.data[~evolving_mask] = 0.0
        self.W_shape_var.grad.data[~evolving_mask] = 0.0
        self.W_rotation.grad.data[~evolving_mask] = 0.0
        self.W_offsets.grad.data[~evolving_mask] = 0.0

        if (global_i > self.merging_marker and 
                global_i < self.merging_marker + self.merging_adaptation_length):
            torch.nn.utils.clip_grad_norm_(self.W_offsets, max_norm=0.001)
            torch.nn.utils.clip_grad_norm_(self.W_scale, max_norm=0.01)
            torch.nn.utils.clip_grad_norm_(self.W_shape_var, max_norm=0.01)
            torch.nn.utils.clip_grad_norm_(self.W_rotation, max_norm=0.01)

    def forward(self, ce, global_i):

        # Add numerical stability to compliance calculation
        H_vec = torch.clamp(self.H, min=self.Emin, max=self.Emax)**self.penal
        compliance = torch.dot(H_vec, ce)

        volfrac_goal = self.volfrac - 0.1*max(0, min(1, (global_i-20)/20))
        
        # Safeguard the loss calculations
        volfrac_loss_pre = torch.nn.functional.relu(
            self.H.mean() - volfrac_goal
        )
        
        gaussian_overlap = torch.nn.functional.relu(
            self.H_splitted_sum - 1.5
        ).mean()

        print("volume: ", self.H.mean())
        print("compliance: ", compliance)
        print("gaussian_overlap: ", gaussian_overlap)

        obj_ce = -compliance*self.compliance_w + volfrac_loss_pre*self.volfrac_w + gaussian_overlap*self.gaussian_overlap_w
        obj_real = compliance*self.compliance_w + volfrac_loss_pre*self.volfrac_w + gaussian_overlap*self.gaussian_overlap_w

        splitted_loss = {
            "gaussian_overlap": gaussian_overlap,
            "volfrac_loss_pre": volfrac_loss_pre,
            "compliance": compliance,
            "obj_ce": obj_ce,
            "obj_real": obj_real,
        }

        return obj_ce, splitted_loss
    
    def compute_merging_pairs(self, kernel):
        # kernel_sigmoids = torch.sigmoid(kernel) 
        # Compute pairwise sums of kernel vectors
        n = kernel.shape[0]  # number of vectors
        pairwise_sums = torch.zeros((n, n), device=kernel.device)

        close_gauss_pairs = []
        close_pair_kernel_sums = []
        self.points_inside_elements = {}

        for i in range(n):
            for j in range(n):
                if j > i:
                    sum_ij = kernel[i] + kernel[j]
                    pairwise_sums[i,j] = sum_ij.max()
                    # print("sum_{}_{}: ".format(i, j), sum_ij.max())
                    if sum_ij.max() > 1.5:
                        close_gauss_pairs.append((i, j))
                        close_pair_kernel_sums.append(sum_ij.max())

                        if i not in self.points_inside_elements.keys():
                            self.points_inside_elements[i] = self.coords[kernel[i] > 0.5]

                        if j not in self.points_inside_elements.keys():
                            self.points_inside_elements[j] = self.coords[kernel[j] > 0.5]
        # print("Pairwise max sums shape:", pairwise_sums.shape)
        # print(pairwise_sums)

        # Convert pairs and sums to tensors and sort by sum values
        if close_gauss_pairs:  # Only process if there are pairs
            pairs_tensor = torch.tensor(close_gauss_pairs, device=pairwise_sums.device)
            sums_tensor = torch.tensor(close_pair_kernel_sums, device=pairwise_sums.device)
            
            # Sort by kernel sums in descending order
            sorted_indices = torch.argsort(sums_tensor, descending=True)
            pairs_tensor = pairs_tensor[sorted_indices]
            sums_tensor = sums_tensor[sorted_indices]
            
            close_gauss_pairs = pairs_tensor.tolist()
            close_pair_kernel_sums = sums_tensor.tolist()

        self.merging_pairs = close_gauss_pairs
        self.merging_kernel_sums = close_pair_kernel_sums

        # print("merging_pairs: ", self.merging_pairs)
    
    def merging(self):
        # TODO: rotation is inncorrectly used (need to take into account sin). 
        # also check the result areas
        # check determinant before splitting
        W_scale = self.W_scale[self.persistent_mask]
        W_shape_var = self.W_shape_var[self.persistent_mask]
        # batch_size = W_scale.shape[0]

        base_scale = (self.scale_max - self.scale_min)*torch.sigmoid(W_scale) + self.scale_min
        sigmas_ratio = (self.sigmas_ratio_max - self.sigmas_ratio_min)*torch.sigmoid(W_shape_var) + self.sigmas_ratio_min
        scale = torch.cat([base_scale, base_scale * sigmas_ratio], dim=1)
        
        covariance_diag = scale.pow(2)
        det_covariance = torch.prod(covariance_diag, dim=1)

        ###############################################################################################################
        persistent_mask_indices = torch.arange(self.persistent_mask.shape[0])[self.persistent_mask]
        self.new_gaussians_mask = torch.zeros_like(self.persistent_mask, dtype=bool)

        areas = np.pi * np.sqrt(det_covariance.detach().cpu().numpy())

        while len(self.merging_pairs) > 0:
            process_pair = self.merging_pairs.pop(0)
            print(process_pair)

            current_index = self.current_marker + 1

            x = torch.cat([self.points_inside_elements[process_pair[0]], self.points_inside_elements[process_pair[1]]])
            x = x.unique(dim=0)
            x_mean = x.mean(dim=0).clone()
            x = x - x_mean
            x = x.detach().cpu().numpy()

            target_area = areas[process_pair[0]] + areas[process_pair[1]]

            center, R, Sigma = fit_ellipsoid(x, target_area)
            center = x_mean - torch.tensor(center)
            R = torch.tensor(R)

            alpha = torch.arccos(R[0, 0])
            if alpha > torch.pi/2:
                alpha = alpha - torch.pi
            elif alpha < -torch.pi/2:
                alpha = alpha + torch.pi

            scale = torch.tensor(Sigma[0])
            ratio = torch.tensor(Sigma[1]/Sigma[0])

            scale = min(
                self.scale_max - 0.1 * (self.scale_max - self.scale_min),
                max(self.scale_min + 0.1 * (self.scale_max - self.scale_min), scale)
            )
            ratio = min(
                self.sigmas_ratio_max - 0.1 * (self.sigmas_ratio_max - self.sigmas_ratio_min),
                max(self.sigmas_ratio_min + 0.1 * (self.sigmas_ratio_max - self.sigmas_ratio_min), ratio)
            )

            scale = torch.tensor(scale)
            ratio = torch.tensor(ratio)

            self.W_rotation.data[current_index] = torch.logit((alpha - self.rotation_min)/(self.rotation_max - self.rotation_min))
            self.W_scale.data[current_index] = torch.logit((scale - self.scale_min)/(self.scale_max - self.scale_min))
            self.W_shape_var.data[current_index] = torch.logit((ratio - self.sigmas_ratio_min)/(self.sigmas_ratio_max - self.sigmas_ratio_min))
            self.W_offsets.data[current_index] = torch.logit((center - self.coord_min)/(self.coord_max - self.coord_min))

            self.persistent_mask[current_index] = True
            self.new_gaussians_mask[current_index] = True
            self.persistent_mask[persistent_mask_indices[process_pair[0]]] = False
            self.persistent_mask[persistent_mask_indices[process_pair[1]]] = False

            # Remove any remaining pairs that contain the merged indices
            self.merging_pairs = [pair for pair in self.merging_pairs if process_pair[0] not in pair and process_pair[1] not in pair]

            self.current_marker += 1
        ##########################################################################################################
        # check determinant before splitting
        # W_scale = self.W_scale[self.persistent_mask]
        # W_shape_var = self.W_shape_var[self.persistent_mask]
        # batch_size = W_scale.shape[0]

        # base_scale = (self.scale_max - self.scale_min)*torch.sigmoid(W_scale) + self.scale_min
        # sigmas_ratio = (self.sigmas_ratio_max - self.sigmas_ratio_min)*torch.sigmoid(W_shape_var) + self.sigmas_ratio_min    
        # scale = torch.cat([base_scale, base_scale * sigmas_ratio], dim=1)
        
        # covariance_diag = scale.pow(2)
        # det_covariance = torch.prod(covariance_diag, dim=1)
        # print("det_covariance: ", det_covariance)
        # print("det_covariance_sum: ", det_covariance.sum())

        ###############################################################################################################

    def update_shape_combination(self, global_i):
        if global_i == self.merging_marker:
            self.merging()

    def get_final_x(self):
        W_scale = self.W_scale[self.persistent_mask]
        W_shape_var = self.W_shape_var[self.persistent_mask] 
        W_rotation = self.W_rotation[self.persistent_mask]
        W_offsets = self.W_offsets[self.persistent_mask]
        batch_size = W_scale.shape[0]

        base_scale = (self.scale_max - self.scale_min)*torch.sigmoid(W_scale) + self.scale_min
        base_scale = base_scale.squeeze()
        sigmas_ratio = (self.sigmas_ratio_max - self.sigmas_ratio_min)*torch.sigmoid(W_shape_var) + self.sigmas_ratio_min    
        scale = torch.cat([torch.ones(batch_size, 1), torch.ones(batch_size, 1) * sigmas_ratio], dim=1)
        rotation = self.rotation_min + (self.rotation_max - self.rotation_min)*torch.sigmoid(W_rotation).view(batch_size)
        offsets = self.coord_min + (self.coord_max - self.coord_min)*torch.sigmoid(W_offsets)

        design_vars = {"scale": base_scale.detach().cpu().numpy().tolist(),
                       "sigmas_ratio": sigmas_ratio.detach().cpu().numpy().tolist(),
                       "rotation": rotation.detach().cpu().numpy().tolist(),
                       "offsets": offsets.detach().cpu().numpy().tolist()}

        return design_vars
    

class CombinedMappingDecoderSDF(torch.nn.Module):
    def __init__(self, coords, volumes, args):
        super().__init__()

        self.saved_model_name = args["args"]["saved_model_name"]
        num_samples = args["args"]["N_g"]

        self.coords = coords.to(torch.float32)   
        self.volumes = volumes.to(torch.float32)
        self.volumes_sum = self.volumes.sum()

        # load stats for latent space
        latent_goal = args["args"]["latent_goal"]
        data = np.load(f"model_weights/{self.saved_model_name}_{latent_goal}_stats.npz")
        self.latent_mins = torch.tensor(data['latent_mins'], dtype=torch.float32) * 1.2
        self.latent_maxs = torch.tensor(data['latent_maxs'], dtype=torch.float32) * 1.2
        self.latent_dim = self.latent_mins.shape[0]


        self.model = AE_DeepSDF_explicit_radius(
            input_dim=17, 
            latent_dim=self.latent_dim, 
            hidden_dim=128, 
            rad_latent_dim=2,
            rad_loss_weight=0.1,
            regularization='l2',   # Use 'l1', 'l2', or None
            reg_weight=1e-3        # Adjust the weight as needed
        )
        self.model.load_state_dict(torch.load(f"model_weights/{self.saved_model_name}.pt"), strict=False)
        self.model.eval()

        # create model for rs_loss
        # checkpoint_path = 'model_weights/rs_loss_conv_combined.ckpt'
        checkpoint_path = 'model_weights/rs_loss_conv.ckpt'
        self.rs_loss_predictor = RSLossPredictorResNet50.load_from_checkpoint(checkpoint_path)
        self.rs_loss_predictor.eval()

        # create variable constraints #######################################################################
        self.sigma_min = 0.002
        self.sigma_max = 0.1

        self.scale_sigma = torch.tensor(0.06)
        self.scale_max = torch.tensor(0.6)
        self.scale_min = torch.tensor(0.05)

        self.rotation_min = -torch.pi/2
        self.rotation_max = torch.pi/2

        self.shape_var_mins = self.latent_mins
        self.shape_var_maxs = self.latent_maxs

        x_min = coords[:, 0].min()
        x_max = coords[:, 0].max()
        y_min = coords[:, 1].min()
        y_max = coords[:, 1].max()

        self.coord_max = torch.tensor([x_max, y_max]).to(torch.float32)
        self.coord_min = torch.tensor([x_min, y_min]).to(torch.float32)

        # sigmas ratio for shape variables (while merging)
        self.sigmas_ratio_max = 2.5
        self.sigmas_ratio_min = 0.5

        # create initial parameters ##############################################################################

        num_samples = args["args"]["N_g"]
        
        # initialize the feature centers according to the grid
        center_x = 0.3
        center_y = 0.1
        x = torch.linspace(0.05, 0.55, args["args"]["N_g_x"])
        y = torch.linspace(0.05, 0.15, args["args"]["N_g_y"])
        x = x + (x.mean() - center_x)
        y = y - (y.mean() - center_y)
        grid_x, grid_y = torch.meshgrid(x, y, indexing='ij')
        dist_means_init = torch.stack([grid_x.flatten(), grid_y.flatten()], dim=1)[:args["args"]["N_g_x"]*args["args"]["N_g_y"]]
        init_num_samples = dist_means_init.shape[0]

        # Create input vector for encoder
        encoder_input = torch.zeros(init_num_samples, 17)
        encoder_input[:, 3] = 1  # Start from circles

        # get initial latent vectors
        _, _, z = self.model(encoder_input)
        z = torch.clamp(z, self.shape_var_mins, self.shape_var_maxs)

        shape_variables = torch.zeros((num_samples, z.shape[1])).to(torch.float32)
        offsets = torch.zeros((num_samples, 2)).to(torch.float32)

        init_num_samples = z.shape[0]

        offsets[:init_num_samples] = dist_means_init
        shape_variables[:init_num_samples] = z

        offsets_values = torch.logit(
            (offsets - self.coord_min) / (self.coord_max - self.coord_min)
        ).type(torch.float32)

        scale_values = torch.logit(
            (torch.ones(num_samples) * self.scale_sigma - self.scale_min) /
            (self.scale_max - self.scale_min)
        ).type(torch.float32)

        rotation_values = torch.logit(
            (torch.zeros((num_samples, 1)) - self.rotation_min) / 
            (self.rotation_max - self.rotation_min)
        ).type(torch.float32)

        shape_var_values = torch.logit(
            (shape_variables - self.shape_var_mins) / (self.shape_var_maxs - self.shape_var_mins)
        ).type(torch.float32)

        self.W_scale = torch.nn.Parameter(scale_values)
        self.W_shape_var = torch.nn.Parameter(shape_var_values)
        self.W_rotation = torch.nn.Parameter(rotation_values)
        self.W_offsets = torch.nn.Parameter(offsets_values)

        left_over_size = num_samples - init_num_samples
        self.current_marker = init_num_samples

        self.persistent_mask = torch.cat(
            [torch.ones(init_num_samples, dtype=bool), torch.zeros(left_over_size, dtype=bool)], dim=0
        )
        self.new_gaussians_mask = torch.zeros_like(self.persistent_mask, dtype=bool)

        # additional parameters #########################################################################################

        self.Emin = args["Emin"]
        self.Emax = args["Emax"]
        self.penal = args["penal"]
        self.volfrac = args["args"]["volfrac"]
        self.volfrac_increment = args["args"]["volfrac_increment"]
        self.volfrac_increment_marker = args["args"]["volfrac_increment_marker"]
        self.volfrac_increment_duration = args["args"]["volfrac_increment_duration"]

        self.compliance_w = args["args"]["compliance_w"]
        self.volfrac_w = args["args"]["volfrac_w"]
        self.gaussian_overlap_w = args["args"]["gaussian_overlap_w"]
        self.gaussian_overlap_scale = args["args"]["gaussian_overlap_scale"]
        self.ff_loss_w = args["args"]["ff_loss_w"]
        self.smooth_k = args["args"]["smooth_k"]
        self.rs_loss = args["args"]["rs_loss"]
        self.rs_loss_start_iter = args["args"]["rs_loss_start_iter"]

        self.merging_markers = args["args"]["merging_markers"]
        self.merging_adaptation = args["args"]["merging_adaptation"]

        self.points_inside_elements = {}
        self.point_ids_inside_elements = {}

    def if_merging_preparation(self, global_i):
        if global_i + 1 in self.merging_markers:
            return True

    def if_merging(self, global_i):
        if global_i in self.merging_markers:
            return True
        
    def if_merging_adaptation(self, global_i):
        in_range = False
        for marker in self.merging_markers:
            if global_i > marker and global_i < marker + self.merging_adaptation:
                in_range = True
        
        return in_range
    
    def compute_merging_pairs(self, kernel_T):
        # Compute pairwise sums of kernel vectors
        kernel = kernel_T.T
        n = kernel.shape[0]  # number of vectors
        pairwise_sums = torch.zeros((n, n), device=kernel.device)

        close_gauss_pairs = []
        close_pair_kernel_sums = []
        self.points_inside_elements = {}

        # print("kernel: ", kernel.shape)

        for i in range(n):
            for j in range(n):
                if j > i:
                    sum_ij = kernel[i] + kernel[j]
                    pairwise_sums[i,j] = sum_ij.max()
                    # print("sum_{}_{}: ".format(i, j), sum_ij.max())
                    if sum_ij.max() > 1.5:
                        close_gauss_pairs.append((i, j))
                        close_pair_kernel_sums.append(sum_ij.max())

                        if i not in self.points_inside_elements.keys():
                            # self.points_inside_elements[i] = self.coords[kernel[i] > 0.5]
                            self.point_ids_inside_elements[i] = kernel[i] > 0.5

                        if j not in self.points_inside_elements.keys():
                            # self.points_inside_elements[j] = self.coords[kernel[j] > 0.5]
                            self.point_ids_inside_elements[j] = kernel[j] > 0.5
        

        # print("merging_sums: ", pairwise_sums)

        # Convert pairs and sums to tensors and sort by sum values
        if close_gauss_pairs:  # Only process if there are pairs
            pairs_tensor = torch.tensor(close_gauss_pairs, device=pairwise_sums.device)
            sums_tensor = torch.tensor(close_pair_kernel_sums, device=pairwise_sums.device)
            
            # Sort by kernel sums in descending order
            sorted_indices = torch.argsort(sums_tensor, descending=True)
            pairs_tensor = pairs_tensor[sorted_indices]
            sums_tensor = sums_tensor[sorted_indices]
            
            close_gauss_pairs = pairs_tensor.tolist()
            close_pair_kernel_sums = sums_tensor.tolist()

        self.merging_pairs = close_gauss_pairs
        self.merging_kernel_sums = close_pair_kernel_sums

        print("merging_pairs: ", self.merging_pairs)

    def merging(self):
        # TODO: rotation is inncorrectly used (need to take into account sin). 
        # also check the result areas
        # check determinant before splitting
        # W_scale = self.W_scale[self.persistent_mask]
        # W_shape_var = self.W_shape_var[self.persistent_mask]
        # batch_size = W_scale.shape[0]

        # base_scale = (self.scale_max - self.scale_min)*torch.sigmoid(W_scale) + self.scale_min
        # sigmas_ratio = (self.sigmas_ratio_max - self.sigmas_ratio_min)*torch.sigmoid(W_shape_var) + self.sigmas_ratio_min
        # scale = torch.cat([base_scale, base_scale * sigmas_ratio], dim=1)
        
        # covariance_diag = scale.pow(2)
        # det_covariance = torch.prod(covariance_diag, dim=1)

        ###############################################################################################################
        persistent_mask_indices = torch.arange(self.persistent_mask.shape[0])[self.persistent_mask]
        self.new_gaussians_mask = torch.zeros_like(self.persistent_mask, dtype=bool)

        # areas = np.pi * np.sqrt(det_covariance.detach().cpu().numpy())

        while len(self.merging_pairs) > 0:
            process_pair = self.merging_pairs.pop(0)
            print(process_pair)

            current_index = self.current_marker + 1

            # x = torch.cat([self.points_inside_elements[process_pair[0]], self.points_inside_elements[process_pair[1]]])
            p_ids_inside = torch.logical_or(self.point_ids_inside_elements[process_pair[0]], self.point_ids_inside_elements[process_pair[1]])
            x = self.coords[p_ids_inside]
            x = x.unique(dim=0)
            x_mean = x.mean(dim=0).clone()
            x = x - x_mean
            x = x.detach().cpu().numpy()

            target_area = self.volumes[p_ids_inside].sum()

            center, R, Sigma = fit_ellipsoid(x, target_area)
            center = x_mean - torch.tensor(center)
            R = torch.tensor(R)

            alpha = torch.arccos(R[0, 0])
            if alpha > torch.pi/2:
                alpha = alpha - torch.pi
            elif alpha < -torch.pi/2:
                alpha = alpha + torch.pi

            scale = torch.tensor(Sigma[0])
            ratio = torch.tensor(Sigma[1]/Sigma[0])

            scale = min(
                self.scale_max - 0.1 * (self.scale_max - self.scale_min),
                max(self.scale_min + 0.1 * (self.scale_max - self.scale_min), scale)
            )
            ratio = min(
                self.sigmas_ratio_max - 0.1 * (self.sigmas_ratio_max - self.sigmas_ratio_min),
                max(self.sigmas_ratio_min + 0.1 * (self.sigmas_ratio_max - self.sigmas_ratio_min), ratio)
            )

            ##############################################################################
            scale = torch.tensor(scale)
            ratio = torch.tensor(ratio)

            # Create input vector for encoder
            encoder_input = torch.zeros(1, 17)
            encoder_input[:, 3] = ratio  # Start from circles

            # get initial latent vectors
            _, _, z = self.model(encoder_input)
            z = torch.clamp(z, self.shape_var_mins, self.shape_var_maxs)
            print("z: ", z.shape)

            self.W_rotation.data[current_index] = torch.logit((alpha - self.rotation_min)/(self.rotation_max - self.rotation_min))
            self.W_scale.data[current_index] = torch.logit((scale - self.scale_min)/(self.scale_max - self.scale_min))
            self.W_shape_var.data[current_index] = torch.logit((z - self.shape_var_mins)/(self.shape_var_maxs - self.shape_var_mins))
            self.W_offsets.data[current_index] = torch.logit((center - self.coord_min)/(self.coord_max - self.coord_min))

            self.persistent_mask[current_index] = True
            self.new_gaussians_mask[current_index] = True
            self.persistent_mask[persistent_mask_indices[process_pair[0]]] = False
            self.persistent_mask[persistent_mask_indices[process_pair[1]]] = False

            # Remove any remaining pairs that contain the merged indices
            self.merging_pairs = [pair for pair in self.merging_pairs if process_pair[0] not in pair and process_pair[1] not in pair]

            self.current_marker += 1

    def get_x(self, global_i):

        W_scale = self.W_scale[self.persistent_mask]
        W_shape_var = self.W_shape_var[self.persistent_mask] 
        W_rotation = self.W_rotation[self.persistent_mask]
        W_offsets = self.W_offsets[self.persistent_mask]
        batch_size = W_scale.shape[0]

        base_scale = (self.scale_max - self.scale_min)*torch.sigmoid(W_scale) + self.scale_min

        shape_var = (self.shape_var_maxs - self.shape_var_mins)*torch.sigmoid(W_shape_var) + self.shape_var_mins    
        rotation = self.rotation_min + (self.rotation_max - self.rotation_min)*torch.sigmoid(W_rotation).view(batch_size)
        offsets = self.coord_min + (self.coord_max - self.coord_min)*torch.sigmoid(W_offsets)

        # print("rotation: ", rotation.shape)
        # print("offsets: ", offsets.shape)
        # print("base_scale: ", base_scale.shape)
        # print("coords: ", self.coords.shape)

        cos_rot = torch.cos(rotation)
        sin_rot = torch.sin(rotation)

        # rotation matrix
        R = torch.stack([
            torch.stack([cos_rot, -sin_rot], dim=-1),
            torch.stack([sin_rot, cos_rot], dim=-1)
        ], dim=-2)

        # Compute inverse covariance
        coords = self.coords.unsqueeze(0).expand(batch_size, -1, -1, -1)
        offsets = offsets[:, None, None, :]
        xy = coords - offsets

        scaled_xy = xy /((base_scale[:, None, None, None])+1e-8)
        scaled_xy_shifted = xy/(self.gaussian_overlap_scale*base_scale[:, None, None, None]+1e-8)

        # print("scaled_xy: ", scaled_xy.shape)
        # print("R: ", R.shape)

        # Apply rotation to coordinates
        rotated_scaled_xy = torch.einsum('bij,bxyj->bxyi', R, scaled_xy)
        rotated_scaled_xy_shifted = torch.einsum('bij,bxyj->bxyi', R, scaled_xy_shifted)
        
        rotated_scaled_xy = rotated_scaled_xy.view(batch_size, -1, 2)
        rotated_scaled_xy_shifted = rotated_scaled_xy_shifted.view(batch_size, -1, 2)

        # Calculate z using pre_inv_covariance (no rotation)
        # rot_scaled_xy = torch.einsum('bxyi,bij,bxyj->bxy', rotated_xy, -0.5 * pre_inv_covariance, rotated_xy)
        kernel = torch.zeros(self.coords.shape[0], batch_size)#.double()
        kernel_shifted = torch.zeros(self.coords.shape[0], batch_size)#.double()

        for i in range(batch_size):
            mask = (rotated_scaled_xy[i].abs().max(dim=-1)[0] < 1)
            # print("mask: ", mask.sum())
            mask_shifted = (rotated_scaled_xy_shifted[i].abs().max(dim=-1)[0] < 1)
            # print("mask_shifted: ", mask_shifted.sum())
            sdfs = self.model.sdf(shape_var[i], rotated_scaled_xy[i, mask, :])
            # print("sdfs: ", sdfs.min(), sdfs.max())
            sdfs_shifted = self.model.sdf(shape_var[i], rotated_scaled_xy_shifted[i, mask_shifted, :])
            kernel[mask, i] = sdfs.squeeze()#.double()
            kernel_shifted[mask_shifted, i] = sdfs_shifted.squeeze()#.double()

        if self.if_merging_preparation(global_i):
            self.compute_merging_pairs(kernel)

        kernel_sum = kernel.sum(dim=1)+1e-8

        self.H = (1 - self.Emin)*torch.sigmoid(-self.smooth_k*(kernel_sum - 0.5)) + self.Emin
        # self.H_splitted = torch.sigmoid(0.1*self.smooth_k*(kernel_shifted - 0.5))
        self.H_splitted = kernel_shifted
        self.H_splitted_sum = self.H_splitted.sum(dim=1)
        self.H_inverted = kernel_sum*0.5

        grid_batch = []

        # if global_i >= self.rs_loss_start_iter:
        #     for i in range(batch_size):
        #         half_side = 1.5
        #         mask = (rotated_scaled_xy[i].abs().max(dim=-1)[0] < half_side)
        #         rotated_scaled_xy_rs = rotated_scaled_xy[i, mask, :]

        #         mapped_grid = _points_to_grid(rotated_scaled_xy_rs, self.H_inverted[mask], half_side=half_side)
        #         grid_batch.append(mapped_grid)
        #         # rs_loss_pred = self.rs_loss_predictor(rotated_scaled_xy_rs)

        #         # Reshape grid for visualization
        #         # grid_size = 40
        #         # grid_2d = mapped_grid.reshape(grid_size, grid_size).detach().numpy()
        #         # grid_2d = mapped_grid[0].detach().numpy()
                
        #         # plt.figure(figsize=(6,6))
        #         # plt.imshow(grid_2d, cmap='viridis', origin='lower')
        #         # plt.colorbar()
        #         # plt.title('Mapped Grid')
        #         # plt.axis('equal')
        #         # plt.show()

        #         # print("rs_loss_pred: ", rs_loss_pred.shape)

        #     rs_loss_grid = torch.stack(grid_batch)
        #     self.rs_loss_vector = self.rs_loss_predictor(rs_loss_grid)

        #     print("rs_loss: ", self.rs_loss_vector.min(), self.rs_loss_vector.max())
        
        # H_splitted_sum_clipped = self.H_splitted_sum.clone()
        # H_splitted_sum_clipped[H_splitted_sum_clipped>0.8] = 0
        # H_splitted_sum_clipped[H_splitted_sum_clipped<0.2] = 0 

        # Compute entropy for H_splitted_sum
        # Add small epsilon to avoid log(0)
        # epsilon = 1e-8
        # p = H_splitted_sum_clipped + epsilon
        # p = p / (p.sum() + epsilon)  # Normalize to get probability distribution
        # entropy = -torch.sum(p * torch.log(p))

        # Create entropy plot
        # plt.figure(figsize=(12, 4))
        # plt.subplot(121)
        # plt.tricontourf(self.coords[:, 0], self.coords[:, 1], p.detach().numpy(), cmap='viridis', levels=20)
        # plt.colorbar()
        # plt.title(f'Normalized H_splitted_sum (Entropy: {entropy.item():.4f})')
        # plt.axis('equal')

        # Create scatter plot of H_splitted_sum using coords
        # plt.figure(figsize=(12, 4))
        # plt.tricontourf(self.coords[:, 0], self.coords[:, 1], self.H_splitted_sum.detach().numpy(), cmap='viridis', levels=20)
        # plt.colorbar()
        # plt.title('H_splitted_sum')
        # plt.axis('equal')

        self.H_splitted_sum_clipped = torch.nn.functional.relu(
            self.H_splitted_sum - 1
        )

        return self.H

    def prepare_grads(self, global_i):

        if self.if_merging_adaptation(global_i) and self.new_gaussians_mask.any():
            print("merging_adaptation ___")
            evolving_mask = self.new_gaussians_mask
        else:
            evolving_mask = self.persistent_mask

        print("evolving_mask: ", evolving_mask)

        self.W_scale.grad.data[~evolving_mask] = 0.0
        self.W_shape_var.grad.data[~evolving_mask] = 0.0
        self.W_rotation.grad.data[~evolving_mask] = 0.0
        self.W_offsets.grad.data[~evolving_mask] = 0.0

        if global_i < 100:
            torch.nn.utils.clip_grad_norm_(self.W_offsets, max_norm=30)
            torch.nn.utils.clip_grad_norm_(self.W_scale, max_norm=20)
            torch.nn.utils.clip_grad_norm_(self.W_shape_var, max_norm=3)
            torch.nn.utils.clip_grad_norm_(self.W_rotation, max_norm=5)
        else:
            torch.nn.utils.clip_grad_norm_(self.W_offsets, max_norm=10)
            torch.nn.utils.clip_grad_norm_(self.W_scale, max_norm=10)
            torch.nn.utils.clip_grad_norm_(self.W_shape_var, max_norm=3)
            torch.nn.utils.clip_grad_norm_(self.W_rotation, max_norm=5)
        
    def compute_ff_loss(self, global_i): # form factor loss
        
        if global_i > 40:
            W_shape_var = self.W_shape_var[self.persistent_mask]
            shape_var = (self.shape_var_maxs - self.shape_var_mins)*torch.sigmoid(W_shape_var) + self.shape_var_mins             
            radius_sum = self.model.radius_sum(shape_var[:, :2])

            print("radius_sum: ", radius_sum.min(), radius_sum.max(), radius_sum.sum())

            return radius_sum.sum()

        #######################################################################

        else:
            return torch.tensor(0.0)
        
    def update_shape_combination(self, global_i):
        if self.if_merging(global_i):
            self.merging()
    
    def forward(self, ce, global_i):

        ff_loss = self.compute_ff_loss(global_i)

        # Add numerical stability to compliance calculation
        H_vec = torch.clamp(self.H, min=self.Emin, max=self.Emax)**self.penal
        compliance = torch.dot(H_vec, ce.float())

        # volfrac_goal = self.volfrac - 0.1*max(0, min(1, (global_i-20)/20))
        volfrac_goal = self.volfrac - self.volfrac_increment * max(
            0, min(1, (global_i - self.volfrac_increment_marker) / self.volfrac_increment_duration)
        )
        volume_goal = self.volumes_sum * volfrac_goal
        # Safeguard the loss calculations
        # TODO: change to vectorproduct with volume vectors
        volfrac_loss_pre = torch.nn.functional.relu(
            self.H.T @ self.volumes - volume_goal
        )
        
        gaussian_overlap = self.H_splitted_sum_clipped.mean()

        print("volume: ", self.H.T @ self.volumes, volfrac_goal, volume_goal, volfrac_loss_pre)
        print("compliance: ", compliance)
        print("gaussian_overlap: ", gaussian_overlap)
        print("ff_loss: ", ff_loss)

        # check the sign
        # Start of Selection
        obj_ce = (
            -compliance * self.compliance_w
            + ff_loss * self.ff_loss_w
            + volfrac_loss_pre * self.volfrac_w
            + gaussian_overlap * self.gaussian_overlap_w
        )
        obj_real = (
            compliance * self.compliance_w
            + ff_loss * self.ff_loss_w
            + volfrac_loss_pre * self.volfrac_w
            + gaussian_overlap * self.gaussian_overlap_w
        )

        if self.rs_loss and global_i >= self.rs_loss_start_iter+1:
            # print("indices: ", len(self.rs_loss_neighbors_indices), self.rs_loss_neighbors_indices[0].shape)
            # rs_loss, _ = compute_rs_loss(self.coords[:, 0], self.coords[:, 1], self.H_splitted_sum,
            #                           self.rs_loss_neighbors_indices, self.rs_loss_inverted_matrices, 
            #                           self.rs_loss_e, self.rs_loss_convex_threshold,
            #                           self.rs_loss_plato_threshold, self.rs_loss_small_cos_dist_threshold,
            #                           visualize=True)
            rs_loss = self.rs_loss_vector.mean()
            rs_loss_weighted = rs_loss * self.rs_loss_w

            obj_real += rs_loss_weighted
            obj_ce += rs_loss_weighted

        else:

            rs_loss = torch.tensor(0.0)

        splitted_loss = {
            "gaussian_overlap": gaussian_overlap,
            "volfrac_loss_pre": volfrac_loss_pre,
            "compliance": compliance,
            "ff_loss": ff_loss,
            "rs_loss": rs_loss,
            "obj_ce": obj_ce,
            "obj_real": obj_real,
        }

        return obj_ce, splitted_loss
