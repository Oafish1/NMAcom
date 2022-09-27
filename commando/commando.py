from itertools import product
from math import prod
import warnings

import anndata as ad
import numpy as np
from scipy import stats
from scipy.optimize import linear_sum_assignment
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE
from sklearn.metrics.pairwise import pairwise_distances
from sklearn.neighbors import KNeighborsClassifier
import torch
import torch.nn as nn  # noqa
import torch.optim as optim
import unioncom.UnionCom as uc
import umap
from unioncom.utils import (
    geodesic_distances,
    init_random_seed,
    joint_probabilities,
)

from .model import edModel, edModelVar
from .nn_funcs import gw_loss, knn_dist, knn_sim, nlma_loss, uc_loss  # noqa
from .utilities import time_logger, uc_visualize, identity, preclass


class ComManDo(uc.UnionCom):
    """
    Adaptation of https://github.com/caokai1073/UnionCom by caokai1073

    P: Correspondence prior matrix
    PF_Ratio: Ratio of priors:assumed correspondence; .5 is equal, 1 is only P
    in_place: Whether to do the calculation in place.  Will save memory but may
        alter original data
    """
    def __init__(
        self,
        P=None,
        match_result=None,
        PF_Ratio=1,
        corr_method='unioncom',
        dist_method='euclidean',
        in_place=False,
        loss_weights=None,
        model_pca='pca',
        model_class=edModelVar,
        pca_dim=None,
        batch_step=False,
        use_f_tilde=True,
        use_early_stop=True,
        min_increment=1e-8,
        max_steps_without_increment=500,
        debug=False,
        **kwargs
    ):
        self.P = P
        self.match_result = match_result
        self.PF_Ratio = PF_Ratio
        self.corr_method = corr_method
        self.dist_method = dist_method
        self.in_place = in_place
        self.loss_weights = loss_weights
        self.model_pca = model_pca
        self.model_class = model_class
        self.pca_dim = pca_dim

        self.batch_step = batch_step
        self.use_f_tilde = use_f_tilde
        self.use_early_stop = use_early_stop
        self.min_increment = min_increment
        self.max_steps_without_increment = max_steps_without_increment

        self.debug = debug

        # Default changes
        # if 'distance_mode' not in kwargs:
        #     kwargs['distance_mode'] = 'spearman'
        if 'project_mode' not in kwargs:
            kwargs['project_mode'] = 'commando'
        if 'log_pd' not in kwargs:
            kwargs['log_pd'] = 500
        if 'lr' not in kwargs:
            kwargs['lr'] = 1e-3

        super().__init__(**kwargs)

    def fit_transform(self, dataset=None):
        """Fit function with ``nlma`` added"""
        distance_modes = [
            # Pairwise
            'euclidean', 'l2', 'l1', 'manhattan', 'cityblock', 'braycurtis',
            'canberra', 'chebyshev', 'correlation', 'cosine', 'dice', 'hamming',
            'jaccard', 'kulsinski', 'mahalanobis', 'matching', 'minkowski',
            'rogerstanimoto', 'russellrao', 'seuclidean', 'sokalmichener',
            'sokalsneath', 'sqeuclidean', 'yule', 'wminkowski', 'nan_euclidean',
            'haversine',
            # Non-pairwise
            'geodesic', 'spearman', 'pearson',
        ]

        if self.integration_type not in ['MultiOmics']:
            raise Exception('integration_type error! Enter MultiOmics.')
        if self.distance_mode not in distance_modes:
            raise Exception('distance_mode error! Enter a correct distance_mode.')
        if self.project_mode not in ('commando', 'tsne'):
            raise Exception("Choose correct project_mode: 'nlma', 'tsne'.")
        if self.integration_type != 'MultiOmics':
            raise Exception(
                "ComManDo is only compatible with integration_type: 'MultiOmics'."
            )
        assert self.model_pca in ('pca', 'umap')

        time = time_logger()
        init_random_seed(self.manual_seed)
        self.device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

        # Test for dataset type (must all be the same)
        self.dataset = dataset
        self.dataset_annotation = None
        if isinstance(self.dataset[0], ad._core.anndata.AnnData):
            self.dataset = [d.X for d in self.dataset]
            self.dataset_annotation = dataset

        # Make a copy
        if not self.in_place:
            self.dataset = self.dataset * 1

        self.dataset_num = len(self.dataset)
        self.col = []
        self.row = []
        for i in range(self.dataset_num):
            self.row.append(np.shape(self.dataset[i])[0])
            self.col.append(np.shape(self.dataset[i])[1])

        # Compute the distance matrix
        self.compute_distances(save_dist=(
            self.project_mode in ['tsne'] or (
                self.match_result is None
                and self.use_f_tilde
            )
        ))
        time.log('Distance')

        # Find correspondence between samples
        if not self.use_f_tilde:
            self.match_result = [np.zeros([d.shape[0] for d in self.dataset])]
        self.match_result = self.match() if self.match_result is None else self.match_result
        pairs_x = []
        pairs_y = []
        for i in range(self.dataset_num - 1):
            cost = np.max(self.match_result[i]) - self.match_result[i]
            row_ind, col_ind = linear_sum_assignment(cost)
            pairs_x.append(row_ind)
            pairs_y.append(col_ind)
        time.log('Correspondence')

        #  Project to common embedding
        if self.project_mode == 'tsne':
            P_joint = []
            for i in range(self.dataset_num):
                P_joint.append(joint_probabilities(self.dist[i], self.perplexity))

            for i in range(self.dataset_num):
                if self.col[i] > 50:
                    self.dataset[i] = PCA(n_components=50).fit_transform(self.dataset[i])
                    self.col[i] = 50

            integrated_data = self.project_tsne(self.dataset, pairs_x, pairs_y, P_joint)
        elif self.project_mode == 'commando':
            match_matrix = [
                [None for j in range(self.dataset_num)]
                for i in range(self.dataset_num)
            ]
            k = 0
            for i, j in product(*(2 * [range(self.dataset_num)])):
                if i == j:
                    # mat = np.eye(self.row[i])
                    mat = None
                elif i > j:
                    mat = match_matrix[j][i].T
                else:
                    mat = self.match_result[k]
                    k += 1
                match_matrix[i][j] = mat
            integrated_data = self.project_commando(match_matrix)
        time.log('Mapping')

        print('-' * 33)
        print('ComManDo Done!')
        time.aggregate()
        print()

        return integrated_data

    def match(self):
        """Find correspondence between multi-omics datasets"""
        print('Device:', self.device)
        cor_pairs = []
        for i in range(self.dataset_num):
            for j in range(i, self.dataset_num):
                if i == j:
                    continue

                print('-' * 33)
                print(f'Find correspondence between Dataset {i + 1} and Dataset {j + 1}')
                if self.corr_method == 'unioncom':
                    F = self.Prime_Dual(
                        [self.dist[i], self.dist[j]],
                        dx=self.col[i],
                        dy=self.col[j],
                    )
                elif self.corr_method == 'commando':
                    F = self.com_corr([self.dist[i], self.dist[j]])
                cor_pairs.append(F)

        print("Finished Matching!")
        return cor_pairs

    def com_corr(self, dist):
        """Estimate correspondence"""
        Kx, Ky = dist
        n, m = np.shape(Kx)[0], np.shape(Ky)[0]
        Kx = torch.Tensor(Kx)
        Ky = torch.Tensor(Ky)

        # Params
        dim = 20
        keep_prob = .35
        epochs = 10001
        epoch_p = 2000

        # Initialize
        a = torch.rand(1, requires_grad=True)
        F = torch.rand(dim, dim, requires_grad=True)
        Tx = torch.rand(dim, n, requires_grad=True)
        Ty = torch.rand(dim, m, requires_grad=True)

        # Step
        print('Clustering')
        optimizer = optim.RMSprop([Tx, Ty], lr=.01)
        for i in range(epochs):
            # loss = (a*Kx - torch.mm(F, torch.mm(Ky, F.T))).square().sum()
            maskx = torch.diag(1.*(torch.rand(n) > (1-keep_prob)))
            masky = torch.diag(1.*(torch.rand(m) > (1-keep_prob)))
            tx = torch.mm(Tx, maskx)
            ty = torch.mm(Ty, masky)
            loss = (
                torch.mm(tx, torch.mm(Kx, tx.T))
                - torch.mm(ty, torch.mm(Ky, ty.T))
            ).square().sum()

            if (i % epoch_p) == 0:
                print(f'loss: {float(loss.cpu().detach())}')

            loss.backward()
            optimizer.step()
            optimizer.zero_grad()

        print('Casting')
        Tx = Tx.detach()
        Ty = Ty.detach()
        optimizer = optim.RMSprop([a, F], lr=.1)
        for i in range(epochs):
            Fc = torch.mm(Tx.T, torch.mm(F, Ty))
            loss = (a*Kx - torch.mm(Fc, torch.mm(Ky, Fc.T))).square().sum()

            if (i % epoch_p) == 0:
                print(f'loss: {float(loss.cpu().detach())}')

            loss.backward()
            optimizer.step()
            optimizer.zero_grad()

        corr = torch.mm(Tx.T, torch.mm(F, Ty)).cpu().detach()
        k = 5
        corr_idx = torch.argsort(corr, dim=1, descending=True)[:, :k]
        corr = torch.zeros(n, m).cpu()
        corr[corr_idx] = 1
        return corr.numpy()

    def Prime_Dual(
        self,
        dist,
        dx=None,
        dy=None,
        verbose=True,
    ):
        """Prime dual combined with Adam algorithm to find the local optimal solution"""
        Kx = dist[0]
        Ky = dist[1]

        # Escape for 1x1 data
        if Kx.shape == (1, 1) and Ky.shape == (1, 1):
            warnings.warn('1x1 distance matrix, escaping...')
            return torch.ones((1, 1)).float().to(self.device)

        N = np.int(np.maximum(Kx.shape[0], Ky.shape[0]))
        Kx = Kx / N
        Ky = Ky / N
        Kx = torch.from_numpy(Kx).float().to(self.device)
        Ky = torch.from_numpy(Ky).float().to(self.device)
        a = np.sqrt(dy/dx)
        m = np.shape(Kx)[0]
        n = np.shape(Ky)[0]

        F = torch.zeros((m, n)).float().to(self.device)
        Im = torch.ones((m, 1)).float().to(self.device)
        In = torch.ones((n, 1)).float().to(self.device)
        Inn = torch.ones((n, n)).float().to(self.device)
        Lambda = torch.zeros((n, 1)).float().to(self.device)
        Mu = torch.zeros((m, 1)).float().to(self.device)
        S = torch.zeros((n, 1)).float().to(self.device)

        pho1 = 0.9
        pho2 = 0.999
        delta = 10e-8
        Fst_moment = torch.zeros((m, n)).float().to(self.device)
        Snd_moment = torch.zeros((m, n)).float().to(self.device)

        i = 0
        while(i < self.epoch_pd):
            # Simplified grad with casting (atol=2e-6)
            # 100k iterations: 5.52e-5, 5.48e-5, 5.50e-5
            FKy = torch.mm(F, Ky)
            grad = (
                4 * torch.mm(FKy, torch.mm(torch.t(F), FKy))
                - 4 * a * torch.mm(Kx, FKy)
                + torch.mm(Mu, torch.t(In))
                + torch.mm(Im, torch.t(Lambda))
                + self.rho * (
                    torch.mm(F, Inn)
                    + torch.mm(
                        Im,
                        # Using premade Imm slows computation
                        torch.mm(torch.t(Im), F)
                        + torch.t(S - 2 * In)
                    )
                )
            )

            i += 1
            Fst_moment = pho1 * Fst_moment + (1 - pho1) * grad
            Snd_moment = pho2 * Snd_moment + (1 - pho2) * grad * grad
            hat_Fst_moment = Fst_moment / (1 - np.power(pho1, i))
            hat_Snd_moment = Snd_moment / (1 - np.power(pho2, i))
            grad = hat_Fst_moment / (torch.sqrt(hat_Snd_moment) + delta)
            F_tmp = F - grad
            F_tmp[F_tmp < 0] = 0

            # update
            F = (1 - self.epsilon) * F + self.epsilon * F_tmp

            # update slack variable
            grad_s = Lambda + self.rho*(torch.mm(torch.t(F), Im) - In + S)
            s_tmp = S - grad_s
            s_tmp[s_tmp < 0] = 0
            S = (1-self.epsilon)*S + self.epsilon*s_tmp

            # update dual variables
            Mu = Mu + self.epsilon*(torch.mm(F, In) - Im)
            Lambda = Lambda + self.epsilon*(torch.mm(torch.t(F), Im) - In + S)

            # if scaling factor changes too fast, we can delay the update
            if self.integration_type == "MultiOmics":
                if i >= self.delay:
                    a = (
                        torch.trace(torch.mm(Kx, torch.mm(torch.mm(F, Ky), torch.t(F))))
                        / torch.trace(torch.mm(Kx, Kx))
                    )

            if verbose and i % self.log_pd == 0:
                if self.integration_type == "MultiOmics":
                    norm2 = torch.norm(a*Kx - torch.mm(torch.mm(F, Ky), torch.t(F)))
                    print("epoch:[{:d}/{:d}] err:{:.4f} alpha:{:.4f}"
                          .format(i, self.epoch_pd, norm2.data.item(), a))
                else:
                    norm2 = torch.norm(dist*F)
                    print("epoch:[{:d}/{:d}] err:{:.4f}"
                          .format(i, self.epoch_pd, norm2.data.item()))

        return F.cpu().detach().numpy()

    def project_commando(self, W):
        """Perform alignment using TSNE-like backend"""
        print('-' * 33)
        print('Train coupled autoencoders')
        assert len(W) == 2, 'Currently only compatible with 2 modalities.'

        # Default vars
        if self.P is None:
            # If not given, assume total alignment iff datasets are same size
            if self.row[0] == self.row[1]:
                self.P = np.eye(self.row[0])
            else:
                self.P = np.zeros((self.row[0], self.row[1]))

        if self.P.shape[0] == self.P.shape[1] and np.abs(self.P - np.eye(self.row[0])).sum() == 0:
            self.perfect_alignment = True
        else:
            self.perfect_alignment = False

        self.P = torch.Tensor(self.P).float().to(self.device)
        self.F = torch.Tensor(W[0][1]).float().to(self.device)

        # Debugging
        # self.epoch_DNN = 500
        # self.lr = .1
        # self.batch_size = 8
        # self.batch_size = 177

        timer = time_logger()
        if self.pca_dim is not None:
            pca_list = []
            pca_inv_list = []
            for dim, data in zip(self.pca_dim, self.dataset):
                if dim is not None:
                    assert min(*data.shape) >= dim, (
                        f'PCA dim must be lower than {min(*data.shape)}, found {dim}')
                    if self.model_pca == 'pca':
                        pca = PCA(n_components=dim)
                    elif self.model_pca == 'umap':
                        pca = umap.UMAP(n_components=dim)
                    elif self.model_pca == 'tsne':
                        # No transform method
                        pca = TSNE(n_components=dim, method='exact')
                    sample = pca.fit_transform(data)
                    pre = preclass(sample, pca=pca)
                    pca_list.append(pre.transform)
                    pca_inv_list.append(pre.inverse_transform)
                else:
                    pre = preclass(data)
                    pca_list.append(pre.transform)
                    pca_inv_list.append(pre.inverse_transform)
            # Python bug?  Doesn't work, overwrites pca
            # NOTE: Just overwrites the pca instance in lambda
            # pca_list = [lambda x: pca.transform(x) for pca in pca_list]

            # Transform datasets (Maybe find less destructive way?)
            self.dataset = [pca_transform(x) for pca_transform, x in zip(pca_list, self.dataset)]
            self.col = [x.shape[1] for x in self.dataset]
        else:
            pca_list = None
            pca_inv_list = None
        self.model = (
            self.model_class(
                self.col,
                self.output_dim,
                preprocessing=pca_list,
                preprocessing_inverse=pca_inv_list,
            ).to(self.device))
        optimizer = optim.Adam(self.model.parameters(), lr=self.lr)

        def sim_diff_func(a, b):
            if self.dist_method == 'cosine':
                # Cosine Similarity
                sim = (
                    torch.mm(a, torch.t(b))
                    / (a.norm(dim=1).reshape(-1, 1) * b.norm(dim=1).reshape(1, -1))
                )
                # return sim, 1-sim
                diff = 1 - sim
                # sim[sim < 0] = 0
                # diff[diff < 0] = 0
                return sim, diff

            elif self.dist_method == 'euclidean':
                # Euclidean Distance
                dist = torch.cdist(a, b, p=2)
                # dist = dist / dist.max()
                sim = 1 / (1+dist)
                sim = 2 * sim - 1  # This one scaling line makes the entire algorithm work
                sim = sim / sim.max()
                return sim, dist

        self.model.train()

        # Convert data
        for i in range(self.dataset_num):
            self.dataset[i] = torch.from_numpy(self.dataset[i]).float().to(self.device)

        # Batch size setup, mainly from UnionCom
        len_dataloader = np.int(np.max(self.row)/self.batch_size)
        if len_dataloader == 0:
            len_dataloader = 1
            self.batch_size = np.max(self.row)

        # Early stopping setup
        best_running_loss = np.inf
        streak = 0

        timer.log('Setup')

        for epoch in range(self.epoch_DNN):
            epoch_loss = 0
            best_batch_loss = np.inf
            for batch_idx in range(len_dataloader):
                batch_loss = 0

                # Random samples
                if self.perfect_alignment:
                    set_rand = np.random.choice(range(self.row[i]), self.batch_size, replace=False)
                random_batch = [
                    set_rand if self.perfect_alignment else
                    np.random.choice(range(self.row[i]), self.batch_size, replace=False)
                    # np.random.choice(range(self.row[i]), self.batch_size - i, replace=False)
                    for i in range(self.dataset_num)
                ]
                data = [self.dataset[i][random_batch[i]] for i in range(self.dataset_num)]

                # P setup
                P = self.P[random_batch[0]][:, random_batch[1]]
                if P.absolute().max() != 0:
                    P /= P.absolute().max()

                # F setup
                F = self.F[random_batch[0]][:, random_batch[1]]
                if F.absolute().max() != 0:
                    F /= F.absolute().max()
                F_inv = 1 - F
                if F_inv.absolute().max() != 0:
                    F_inv /= F_inv.absolute().max()

                timer.log('Get subset samples')

                # Aggregate correspondence
                corr = self.PF_Ratio * P + (1-self.PF_Ratio) * F

                # Run model
                embedded, combined, reconstructed, mus, stds = self.model(*data, corr=corr)
                timer.log('Run model')

                # Loss bookkeeping
                losses = []
                losses_names = []

                # # ELBO (VAE)
                # # kl
                # kl_list = []
                # for z, mu, std in zip(embedded, mus, stds):
                #     p = torch.distributions.Normal(torch.zeros_like(mu), torch.ones_like(std))
                #     q = torch.distributions.Normal(mu, std)
                #
                #     log_qzx = q.log_prob(z)
                #     log_pz = p.log_prob(z)
                #
                #     kl = log_qzx - log_pz
                #     kl_list.append(kl.sum(-1))
                #
                # # gaus
                # rec_list = []
                # for dat, rec, ls in zip(data, reconstructed, self.model.log_scale):
                #     scale = torch.exp(ls)
                #     dist = torch.distributions.Normal(rec, scale)
                #
                #     log_pxz = dist.log_prob(dat)
                #     rec_list.append(log_pxz.sum(-1))
                #
                # # if (epoch % 500) == 0:
                # #     print(self.model.log_scale)
                # #     print(torch.exp(log_pxz.max()))
                # #     print()
                #
                # elbo = sum([(kl - rec).mean() for kl, rec in zip(kl_list, rec_list)])
                # losses.append(elbo)
                # timer.log('ELBO')

                # KL Loss (VAE)
                kl_loss = sum(
                    -.5 * torch.sum(
                        1
                        + torch.log(stds[i])
                        - torch.square(mus[i])
                        - torch.square(stds[i])
                    )  / data[i].shape[0]
                    for i in range(self.dataset_num)
                )
                # https://stats.stackexchange.com/questions/332179/how-to-weight-kld-loss-vs-reconstruction-loss-in-variational-auto-encoder
                c = self.epoch_DNN / 2  # Midpoint
                kl_anneal = 1 / ( 1 + np.exp( - 5 * (epoch - c) / c ) )
                losses.append(1e-3 * kl_anneal * kl_loss)
                losses_names.append('KL')
                timer.log('KL Loss')
                # asdf revise early stop for late-increasing losses

                # MSE Reconstruction error (AE/VAE)
                reconstruction_loss = sum(
                    (reconstructed[i] - data[i]).square().sum() / prod(data[i].shape)
                    for i in range(self.dataset_num)
                )
                losses.append(reconstruction_loss)
                losses_names.append('Rec')
                timer.log('Reconstruction Loss')

                # Difference
                # csim, cdiff = sim_diff_func(embedded[0], embedded[1])
                # csim0, cdiff0 = sim_diff_func(embedded[0], embedded[0])
                # csim1, cdiff1 = sim_diff_func(embedded[1], embedded[1])
                cosim0, codiff0 = sim_diff_func(embedded[0], combined[0])
                cosim1, codiff1 = sim_diff_func(embedded[1], combined[1])
                # comsim1, comdiff1 = sim_diff_func(combined[0], combined[1])
                timer.log('Difference calculation')

                # Cosine Loss (asdf update with P)
                cosine_loss = torch.diag(
                    codiff0 / data[0].shape[0]
                    + codiff1 / data[1].shape[0]).sum()
                losses.append(cosine_loss)
                losses_names.append('CosSim')
                timer.log('Cosine Loss')

                # F Reconstruction Loss
                F_est = torch.square(
                    combined[0] - torch.mm(F, combined[1])
                ).sum() / prod(combined[0].shape)
                losses.append(F_est)
                losses_names.append('F')
                timer.log('F Loss')

                # # Cross Loss
                # cross_loss = comdiff1 * F
                # cross_loss = cross_loss.sum() / prod(F.shape)
                # losses.append(cross_loss)
                # timer.log('Cross Loss')

                # # Alignment loss
                # if P.absolute().sum() != 0:
                #     weighted_P_cdiff = cdiff * P
                #     alignment_loss = weighted_P_cdiff.absolute().sum() / P.absolute().sum()
                #     losses.append(alignment_loss)
                #     timer.log('Aligned loss')
                #
                # # Cross loss using F
                # weighted_F_cdiff = cdiff * F
                # cross_loss = weighted_F_cdiff.absolute().sum() / F.absolute().sum()
                # losses.append(cross_loss)
                # timer.log('F-cross loss')
                #
                # # Inverse cross loss using F
                # weighted_F_inv_csim = csim * F_inv
                # inv_cross_loss = weighted_F_inv_csim.absolute().sum() / prod(F.shape)  # F_inv.absolute().sum()
                # losses.append(inv_cross_loss)
                # timer.log('F-inv-cross loss')

                # Record loss
                if self.loss_weights is not None:
                    assert len(losses) == len(self.loss_weights), (
                        f'There are {len(losses)} losses and {len(self.loss_weights)} weights')
                    batch_loss = sum([lo * wt for lo, wt in zip(losses, self.loss_weights)])
                else:
                    batch_loss = sum(losses)
                epoch_loss += batch_loss / len_dataloader
                if batch_loss < best_batch_loss:
                    best_batch_loss = batch_loss

                # Append losses
                batch_loss.backward()

                if self.batch_step:
                    # Step
                    optimizer.step()
                    optimizer.zero_grad()
                    timer.log('Step')

            if not self.batch_step:
                # Step
                optimizer.step()
                optimizer.zero_grad()
                timer.log('Step')

            # CLI Printing
            if (epoch+1) % self.log_DNN == 0:
                print(f'epoch:[{epoch+1:d}/{self.epoch_DNN}]: loss:{epoch_loss.data.item():4f}')

            # Debug Printing
            if (epoch+1) % 100 == 0 and self.debug:
                if self.loss_weights is not None:
                    print('  '.join([f'{losses_names[i]}: {lo.detach().cpu() * wt:.4f}'
                                     for i, (lo, wt) in enumerate(zip(losses, self.loss_weights))]))
                else:
                    print('  '.join([f'{losses_names[i]}: {lo.detach().cpu():.4f}'
                                     for i, lo in enumerate(losses)]))

            # Early stopping
            if self.batch_step:
                active_loss = best_batch_loss
            else:
                active_loss = epoch_loss
            epsilon = best_running_loss - active_loss
            if epsilon > self.min_increment:
                best_running_loss = active_loss
                streak = 0
            else:
                streak += 1
            if streak >= self.max_steps_without_increment and self.use_early_stop:
                break

        self.model.eval()
        corr_P = self.P / self.P.absolute().max()
        corr_F = self.F / self.F.absolute().max()
        corr = self.PF_Ratio * corr_P + (1-self.PF_Ratio) * corr_F
        integrated_data = self.model(*self.dataset, corr=corr)[0]
        integrated_data = [d.detach().cpu().numpy() for d in integrated_data]
        timer.log('Output')
        print("Finished Mapping!")
        # timer.aggregate()
        return integrated_data

    def modal_predict(self, data, modality):
        """Predict the opposite modality from dataset ``data`` in modality ``modality``"""
        assert self.model is not None, 'Model must be trained before modal prediction.'

        to_modality = (modality + 1) % self.dataset_num
        return self.model.impute(data, compose=[modality, to_modality])

    def transform(self, dataset, corr=None, pre_transformed=False):
        """Transform data using an already trained model"""
        if corr is None:
            if dataset[0].shape[0] == dataset[1].shape[0]:
                corr = torch.eye(dataset[0].shape[0])
            else:
                corr = torch.zeros((dataset[0].shape[0], dataset[1].shape[0]))
        if not pre_transformed:
            dataset = [self.model.preprocessing[i](dataset[i]) for i in range(len(dataset))]
        dataset = [torch.Tensor(d) for d in dataset]
        integrated = self.model(*dataset, corr=corr)[0]
        return [d.detach().cpu().numpy() for d in integrated]

    def compute_distances(self, save_dist=True):
        """Helper function to compute distances for each dataset"""
        if save_dist:
            self.dist = []
        print('Shape of Raw data')
        for i in range(self.dataset_num):
            print('Dataset {}:'.format(i), np.shape(self.dataset[i]))
            # self.dataset[i] = (
            #     (self.dataset[i] - np.min(self.dataset[i]))
            #     / (np.max(self.dataset[i]) - np.min(self.dataset[i]))
            # )

            if self.distance_mode == 'geodesic':
                def distance_function(df):
                    distances = geodesic_distances(df, self.kmax)
                    return np.array(distances)
            elif self.distance_mode == 'spearman':
                # Note: Method may not work if empty features are given
                def distance_function(df):
                    if df.shape[0] == 1:
                        distances = np.array([0])
                    else:
                        distances, _ = stats.spearmanr(df, axis=1)
                        if np.isnan(distances).any():
                            raise Exception(
                                'Data is not well conditioned for spearman method '
                                '(scipy.stats.spearmanr returned ``np.nan``)'
                            )
                    if len(distances.shape) == 0:
                        distances = np.array([[1, distances], [distances, 1]])
                    return (1 - np.array(distances)) / 2
            elif self.distance_mode == 'pearson':
                # Note: Method may not work if empty features are given
                # ASDDDF: Compatibility with dense matrices
                def distance_function(df):
                    if df.shape[0] == 1:
                        return np.array([0])
                    distances = np.corrcoef(df.toarray())
                    if len(distances.shape) == 0:
                        distances = np.array([[1, distances], [distances, 1]])
                    return (1 - np.array(distances)) / 2
            else:
                def distance_function(df):
                    return pairwise_distances(df, metric=self.distance_mode)

            self.distance_function = distance_function
            if save_dist:
                distances = self.distance_function(self.dataset[i])
                self.dist.append(distances)

    def test_closer(
        self,
        integrated_data,
        distance_metric=lambda x: pairwise_distances(x, metric='euclidean'),
    ):
        """Test fraction of samples closer than the true match"""
        # ASDDF: 3+ datasets and non-aligned data
        assert len(integrated_data) == 2, 'Two datasets are supported for FOSCTTM'

        if distance_metric is None:
            distance_metric = self.distance_function
        distances = distance_metric(np.concatenate(integrated_data, axis=0))
        size = integrated_data[0].shape[0]
        raw_count_closer = 0
        for i in range(size):
            # A -> B
            local_dist = distances[i][size:]
            raw_count_closer += np.sum(local_dist < local_dist[i])
            # B -> A
            local_dist = distances[size+i][:size]
            raw_count_closer += np.sum(local_dist < local_dist[i])
        foscttm = raw_count_closer / (2 * size**2)
        print(f'foscttm: {foscttm}')
        return foscttm

    def test_label_dist(
        self,
        integrated_data,
        datatype,
        distance_metric=lambda x: pairwise_distances(x, metric='euclidean'),
        verbose=True,
    ):
        """Test average distance by label"""
        # ASDDF: 3+ datasets
        assert len(integrated_data) == 2, 'Two datasets are supported for ``label_dist``'

        if distance_metric is None:
            distance_metric = self.distance_function
        data = np.concatenate(integrated_data, axis=0)
        labels = np.concatenate(datatype)

        # Will double-count aligned sample
        average_representation = {}
        for label in np.unique(labels):
            average_representation[label] = np.average(data[labels == label, :], axis=0)
        dist = distance_metric(np.array(list(average_representation.values())))
        if verbose:
            print(f'Inter-label distances ({list(average_representation.keys())}):')
            print(dist)
        return np.array(list(average_representation.keys())), dist

    def test_LabelTA(self, integrated_data, datatype, k=None, return_k=False):
        """Modified version of UnionCom ``test_LabelTA`` to return acc"""
        if k is None:
            # Set to 20% of avg class size if no k provided
            total_size = min(*[len(d) for d in datatype])
            num_classes = len(np.unique(np.concatenate(datatype)).flatten())
            k = int(.2 * total_size / num_classes)
        knn = KNeighborsClassifier(n_neighbors=k)
        knn.fit(integrated_data[1], datatype[1])
        type1_predict = knn.predict(integrated_data[0])
        count = 0
        for label1, label2 in zip(type1_predict, datatype[0]):
            if label1 == label2:
                count += 1
        acc = count / len(datatype[0])
        print(f"label transfer accuracy: {acc}")
        if return_k:
            return acc, k
        return acc

    def Visualize(self, data, integrated_data, datatype=None, mode=None):
        """In-class API for modified visualization function"""
        uc_visualize(data, integrated_data, datatype=datatype, mode=mode)

    def save_model(self, f):
        torch.save(self.model, f)

    def load_model(self, f):
        self.model = torch.load(f)
        self.dataset_num = self.model.num_modalities
