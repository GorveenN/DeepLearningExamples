# Copyright (c) 2019 NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch
import torch.nn as nn
import torch.nn.functional as F

from utils.log_uniform_sampler import LogUniformSampler
from utils.log_uniform_sampler import sample_logits
from utils.proj_adaptive_softmax import ProjectedAdaptiveLogSoftmax

from pruning.prunection import Prunection
from pruning.prunode import Prunode

class PositionalEmbedding(nn.Module):
    def __init__(self, demb):
        super(PositionalEmbedding, self).__init__()

        self.demb = demb

        inv_freq = 1 / (10000 ** (torch.arange(0.0, demb, 2.0) / demb))
        self.register_buffer('inv_freq', inv_freq)

    def forward(self, pos_seq, bsz=None):
        sinusoid_inp = torch.ger(pos_seq, self.inv_freq)
        pos_emb = torch.cat([sinusoid_inp.sin(), sinusoid_inp.cos()], dim=-1)

        if bsz is not None:
            return pos_emb[:, None, :].expand(-1, bsz, -1)
        else:
            return pos_emb[:, None, :]


class PositionwiseFF(nn.Module):
    def __init__(self, d_model, d_inner, dropout, last=False, pre_lnorm=False):
        super(PositionwiseFF, self).__init__()

        self.d_model = d_model
        self.d_inner = d_inner
        self.dropout = dropout

        lin1 = Prunode(lambda i,o: nn.Linear(i,o))

        if(not last):
            lin2 = Prunode(lambda i,o: nn.Linear(i, o))

            self.CoreNet = nn.Sequential(
                lin1,
                nn.ReLU(inplace=True),
                Prunection([nn.Sequential(lin1)], [lin2], self.d_inner),
                nn.Dropout(dropout),
                lin2,
                Prunection([nn.Sequential(lin2)], [], self.d_model),
                nn.Dropout(dropout),
            )
        else:
            lin2 = Prunode(lambda i,o: nn.Linear(i, d_model))
            self.CoreNet = nn.Sequential(
                lin1,
                nn.ReLU(inplace=True),
                Prunection([nn.Sequential(lin1)], [lin2], self.d_inner),
                nn.Dropout(dropout),
                lin2,
                nn.Dropout(dropout)
            )

        self.layer_norm = nn.LayerNorm(d_model)

        self.pre_lnorm = pre_lnorm

    def forward(self, inp):
        if self.pre_lnorm:
            # layer normalization + positionwise feed-forward
            core_out = self.CoreNet(self.layer_norm(inp))

            # residual connection
            output = core_out + inp
        else:
            # positionwise feed-forward
            core_out = self.CoreNet(inp)

            # residual connection + layer normalization
            output = self.layer_norm(inp + core_out)

        return output

    # sets the last linear's paired gates to given
    def set_paired_out(self, gates):
        # beautiful hardcoded 4
        self.CoreNet[5].set_paired(gates)

    def set_output_gate_layer(self, layer):
        # this is against OOP
        self.CoreNet[5].output_layers.append(layer)
        self.CoreNet[5]._init_prunode()


class MultiHeadAttn(nn.Module):
    def __init__(self, n_head, d_model, d_head, dropout, dropatt=0,
                 pre_lnorm=False):
        super(MultiHeadAttn, self).__init__()

        self.n_head = n_head
        self.d_model = d_model
        self.d_head = d_head
        self.dropout = dropout

        self.q_net = nn.Linear(d_model, n_head * d_head, bias=False)
        self.kv_net = nn.Linear(d_model, 2 * n_head * d_head, bias=False)

        self.drop = nn.Dropout(dropout)
        self.dropatt = nn.Dropout(dropatt)
        self.o_net = nn.Linear(n_head * d_head, d_model, bias=False)

        self.layer_norm = nn.LayerNorm(d_model)

        self.scale = 1 / (d_head ** 0.5)

        self.pre_lnorm = pre_lnorm

    def forward(self, h, attn_mask=None, mems=None):
        # multihead attention
        # [hlen x bsz x n_head x d_head]

        if mems is not None:
            c = torch.cat([mems, h], 0)
        else:
            c = h

        if self.pre_lnorm:
            # layer normalization
            c = self.layer_norm(c)

        head_q = self.q_net(h)
        head_k, head_v = torch.chunk(self.kv_net(c), 2, -1)

        head_q = head_q.view(h.size(0), h.size(1), self.n_head, self.d_head)
        head_k = head_k.view(c.size(0), c.size(1), self.n_head, self.d_head)
        head_v = head_v.view(c.size(0), c.size(1), self.n_head, self.d_head)

        # [bsz x n_head x qlen x klen]
        attn_score = torch.einsum('ibnd,jbnd->bnij', (head_q, head_k))
        attn_score.mul_(self.scale)
        if attn_mask is not None and attn_mask.any().item():
            if attn_mask.dim() == 2:
                attn_score.masked_fill_(attn_mask[None, None, :, :], -float('inf'))
            elif attn_mask.dim() == 3:
                attn_score.masked_fill_(attn_mask[:, None, :, :], -float('inf'))

        # [bsz x qlen x klen x n_head]
        attn_prob = F.softmax(attn_score, dim=3)
        attn_prob = self.dropatt(attn_prob)

        # [bsz x n_head x qlen x klen] * [klen x bsz x n_head x d_head] -> [qlen x bsz x n_head x d_head]
        attn_vec = torch.einsum('bnij,jbnd->ibnd', (attn_prob, head_v))
        attn_vec = attn_vec.contiguous().view(
            attn_vec.size(0), attn_vec.size(1), self.n_head * self.d_head)

        # linear projection
        attn_out = self.o_net(attn_vec)
        attn_out = self.drop(attn_out)

        if self.pre_lnorm:
            # residual connection
            output = h + attn_out
        else:
            # residual connection + layer normalization
            output = self.layer_norm(h + attn_out)

        return output


class RelMultiHeadAttn(nn.Module):
    def __init__(self, n_head, d_model, d_head, dropout, dropatt=0,
                 tgt_len=None, ext_len=None, mem_len=None, pre_lnorm=False, first=False):
        super(RelMultiHeadAttn, self).__init__()

        self.n_head = n_head
        self.d_model = d_model
        self.d_head = d_head
        self.dropout = dropout

        if not first:
            qkv = Prunode(lambda i,o: nn.Linear(i, o, bias=False))
        else:
            qkv = Prunode(lambda i,o: nn.Linear(d_model, o, bias=False)) 
       
        self.qkv_net = nn.Sequential(
            qkv,
            # nn.Linear(d_model, 3 * n_head * d_head, bias=False),
            Prunection([nn.Sequential(qkv)], [], 3 * n_head * d_head, period=n_head * d_head, chunk=d_head)
        )

        self.drop = nn.Dropout(dropout)
        self.dropatt = nn.Dropout(dropatt)

        # TODO: before o_net there's a v_net, so we should probably include it in layers preceding o_net
        o = Prunode(lambda i,o: nn.Linear(n_head * d_head, o, bias=False))
        self.o_net = nn.Sequential(
            o,
            # nn.Linear(n_head * d_head, d_model, bias=False),
            Prunection([nn.Sequential(o)], [], d_model)
        )

        self.layer_norm = nn.LayerNorm(d_model)

        self.scale = 1 / (d_head ** 0.5)

        self.pre_lnorm = pre_lnorm

    def set_o_out(self, layer):
        self.o_net[1].output_layers.append(layer)
        self.o_net[1]._init_prunode()

    def set_paired_to_o(self, gates):
        self.o_net[1].set_paired(gates)

    def _parallelogram_mask(self, h, w, left=False):
        mask = torch.ones((h, w)).byte()
        m = min(h, w)
        mask[:m, :m] = torch.triu(mask[:m, :m])
        mask[-m:, -m:] = torch.tril(mask[-m:, -m:])

        if left:
            return mask.bool()
        else:
            return mask.flip(0).bool()

    def _shift(self, x, qlen, klen, mask, left=False):
        if qlen > 1:
            zero_pad = torch.zeros((x.size(0), qlen-1, x.size(2), x.size(3)),
                                   device=x.device, dtype=x.dtype)
        else:
            zero_pad = torch.zeros(0, device=x.device, dtype=x.dtype)

        if left:
            mask = mask.flip(1)
            x_padded = torch.cat([zero_pad, x], dim=1).expand(qlen, -1, -1, -1)
        else:
            x_padded = torch.cat([x, zero_pad], dim=1).expand(qlen, -1, -1, -1)

        x = x_padded.masked_select(mask[:, :, None, None]) \
                    .view(qlen, klen, x.size(2), x.size(3))

        return x

    def _rel_shift(self, x, zero_triu=False):
        zero_pad = torch.zeros((x.size(0), x.size(1), x.size(2), 1),
                               device=x.device, dtype=x.dtype)
        x_padded = torch.cat([zero_pad, x], dim=3)

        x_padded = x_padded.view(x.size(0), x.size(1), x.size(3) + 1, x.size(2))

        x = x_padded[:, :, 1:].view_as(x)

        if zero_triu:
            ones = torch.ones((x.size(2), x.size(3)))
            x = x * torch.tril(ones, x.size(3) - x.size(2))[None, None, :, :]

        return x

    def forward(self, w, r, attn_mask=None, mems=None):
        raise NotImplementedError


class RelPartialLearnableMultiHeadAttn(RelMultiHeadAttn):
    def __init__(self, *args, **kwargs):
        super(RelPartialLearnableMultiHeadAttn, self).__init__(*args, **kwargs)

        r = Prunode(lambda i,o: nn.Linear(self.d_model, o, bias=False))
        self.r_net = nn.Sequential(
            # nn.Linear(self.d_model, self.n_head * self.d_head, bias=False),
            r,
            Prunection([nn.Sequential(r)], [], self.n_head * self.d_head, chunk=self.d_head)
        )

        self.qkv_net[1].set_paired([self.r_net[1]])
        self.bias_mask = None

    def forward(self, w, r, r_w_bias, r_r_bias, attn_mask=None, mems=None):
        qlen, rlen, bsz = w.size(0), r.size(0), w.size(1)

        # adjusting r_w_bias and r_r_bias to pruned modules
        #mask = (self.r_net[1].total_mask == True)
        #shape_r_w_bias = r_w_bias.shape
        #r_w_bias = torch.flatten(r_w_bias)[mask].view(shape_r_w_bias)
        #shape_r_r_bias = r_r_bias.shape
        #r_r_bias = torch.flatten(r_r_bias)[mask].view(shape_r_r_bias)
        r_w_bias = r_w_bias.flatten()[self.r_net[1].total_mask == True].view(-1, self.d_head)
        r_r_bias = r_r_bias.flatten()[self.r_net[1].total_mask == True].view(-1, self.d_head)

        if mems is not None:
            cat = torch.cat([mems, w], 0)
            if self.pre_lnorm:
                w_heads = self.qkv_net(self.layer_norm(cat))
            else:
                w_heads = self.qkv_net(cat)
            r_head_k = self.r_net(r)

            w_head_q, w_head_k, w_head_v = torch.chunk(w_heads, 3, dim=-1)
            w_head_q = w_head_q[-qlen:]
        else:
            if self.pre_lnorm:
                w_heads = self.qkv_net(self.layer_norm(w))
            else:
                w_heads = self.qkv_net(w)
            r_head_k = self.r_net(r)

            w_head_q, w_head_k, w_head_v = torch.chunk(w_heads, 3, dim=-1)

        klen = w_head_k.size(0)

        w_head_q = w_head_q.view(qlen, bsz, -1, self.d_head)  # qlen x bsz x n_head x d_head
        w_head_k = w_head_k.view(klen, bsz, -1, self.d_head)  # klen x bsz x n_head x d_head
        w_head_v = w_head_v.view(klen, bsz, -1, self.d_head)  # klen x bsz x n_head x d_head

        r_head_k = r_head_k.view(rlen, -1, self.d_head)       # qlen x n_head x d_head


        # compute attention score
        rw_head_q = w_head_q + r_w_bias                                # qlen x bsz x n_head x d_head
        AC = torch.einsum('ibnd,jbnd->bnij', (rw_head_q, w_head_k))    # bsz x n_head x qlen x klen

        rr_head_q = w_head_q + r_r_bias
        BD = torch.einsum('ibnd,jnd->bnij', (rr_head_q, r_head_k))     # bsz x n_head x qlen x klen
        BD = self._rel_shift(BD)

        # [bsz x n_head x qlen x klen]
        attn_score = AC + BD
        attn_score.mul_(self.scale)

        # compute attention probability
        if attn_mask is not None and attn_mask.any().item():
            if attn_mask.dim() == 2:
                attn_score.masked_fill_(attn_mask[None, None, :, :], -float('inf'))
            elif attn_mask.dim() == 3:
                attn_score.masked_fill_(attn_mask[:, None, :, :], -float('inf'))

        # [bsz x n_head x qlen x klen]
        attn_prob = F.softmax(attn_score, dim=3)
        attn_prob = self.dropatt(attn_prob)

        # compute attention vector
        attn_vec = torch.einsum('bnij,jbnd->ibnd', (attn_prob, w_head_v))

        # [qlen x bsz x n_head x d_head]
        attn_vec = attn_vec.contiguous().view(attn_vec.size(0), attn_vec.size(1), -1)

        # linear projection
        attn_out = self.o_net(attn_vec)
        attn_out = self.drop(attn_out)

        if self.pre_lnorm:
            # residual connection
            output = w + attn_out
        else:
            # residual connection + layer normalization
            output = self.layer_norm(w + attn_out)

        return output


class RelLearnableMultiHeadAttn(RelMultiHeadAttn):
    def __init__(self, *args, **kwargs):
        super(RelLearnableMultiHeadAttn, self).__init__(*args, **kwargs)

    def forward(self, w, r_emb, r_w_bias, r_bias, attn_mask=None, mems=None):
        # r_emb: [klen, n_head, d_head], used for term B
        # r_w_bias: [n_head, d_head], used for term C
        # r_bias: [klen, n_head], used for term D

        qlen, bsz = w.size(0), w.size(1)

        if mems is not None:
            cat = torch.cat([mems, w], 0)
            if self.pre_lnorm:
                w_heads = self.qkv_net(self.layer_norm(cat))
            else:
                w_heads = self.qkv_net(cat)
            w_head_q, w_head_k, w_head_v = torch.chunk(w_heads, 3, dim=-1)

            w_head_q = w_head_q[-qlen:]
        else:
            if self.pre_lnorm:
                w_heads = self.qkv_net(self.layer_norm(w))
            else:
                w_heads = self.qkv_net(w)
            w_head_q, w_head_k, w_head_v = torch.chunk(w_heads, 3, dim=-1)

        klen = w_head_k.size(0)

        w_head_q = w_head_q.view(qlen, bsz, self.n_head, self.d_head)
        w_head_k = w_head_k.view(klen, bsz, self.n_head, self.d_head)
        w_head_v = w_head_v.view(klen, bsz, self.n_head, self.d_head)

        if klen > r_emb.size(0):
            r_emb_pad = r_emb[0:1].expand(klen-r_emb.size(0), -1, -1)
            r_emb = torch.cat([r_emb_pad, r_emb], 0)
            r_bias_pad = r_bias[0:1].expand(klen-r_bias.size(0), -1)
            r_bias = torch.cat([r_bias_pad, r_bias], 0)
        else:
            r_emb = r_emb[-klen:]
            r_bias = r_bias[-klen:]

        r_bias = r_bias.t()

        # compute attention score
        rw_head_q = w_head_q + r_w_bias[None]                        # qlen x bsz x n_head x d_head

        AC = torch.einsum('ibnd,jbnd->bnij', (rw_head_q, w_head_k))  # bsz x n_head x qlen x klen
        B_ = torch.einsum('ibnd,jnd->bnij', (w_head_q, r_emb))       # bsz x n_head x qlen x klen
        D_ = r_bias[None, :, None, :]                                # 1   x n_head x    1 x klen
        BD = self._rel_shift(B_ + D_)

        # [bsz x qlen x klen x n_head]
        attn_score = AC + BD
        attn_score.mul_(self.scale)

        # compute attention probability
        if attn_mask is not None and attn_mask.any().item():
            if attn_mask.dim() == 2:
                attn_score.masked_fill_(attn_mask[None, None, :, :], -float('inf'))
            elif attn_mask.dim() == 3:
                attn_score.masked_fill_(attn_mask[:, None, :, :], -float('inf'))

        # [bsz x n_head x qlen x klen]
        attn_prob = F.softmax(attn_score, dim=3)
        attn_prob = self.dropatt(attn_prob)

        # compute attention vector
        attn_vec = torch.einsum('bnij,jbnd->ibnd', (attn_prob, w_head_v))

        # [qlen x bsz x n_head x d_head]
        attn_vec = attn_vec.contiguous().view(
            attn_vec.size(0), attn_vec.size(1), self.n_head * self.d_head)

        # linear projection
        attn_out = self.o_net(attn_vec)
        attn_out = self.drop(attn_out)

        if self.pre_lnorm:
            # residual connection
            output = w + attn_out
        else:
            # residual connection + layer normalization
            output = self.layer_norm(w + attn_out)

        return output


class DecoderLayer(nn.Module):
    def __init__(self, n_head, d_model, d_head, d_inner, dropout, **kwargs):
        super(DecoderLayer, self).__init__()

        self.dec_attn = MultiHeadAttn(n_head, d_model, d_head, dropout, **kwargs)
        self.pos_ff = PositionwiseFF(d_model, d_inner, dropout,
                                     pre_lnorm=kwargs.get('pre_lnorm'))

    def forward(self, dec_inp, dec_attn_mask=None, mems=None):

        output = self.dec_attn(dec_inp, attn_mask=dec_attn_mask,
                               mems=mems)
        output = self.pos_ff(output)

        return output


class RelLearnableDecoderLayer(nn.Module):
    def __init__(self, n_head, d_model, d_head, d_inner, dropout,
                 **kwargs):
        super(RelLearnableDecoderLayer, self).__init__()

        self.dec_attn = RelLearnableMultiHeadAttn(n_head, d_model, d_head,
                                                  dropout, **kwargs)
        self.pos_ff = PositionwiseFF(d_model, d_inner, dropout,
                                     pre_lnorm=kwargs.get('pre_lnorm'))

    def forward(self, dec_inp, r_emb, r_w_bias, r_bias, dec_attn_mask=None, mems=None):

        output = self.dec_attn(dec_inp, r_emb, r_w_bias, r_bias,
                               attn_mask=dec_attn_mask,
                               mems=mems)
        output = self.pos_ff(output)

        return output


class RelPartialLearnableDecoderLayer(nn.Module):
    def __init__(self, n_head, d_model, d_head, d_inner, dropout, last=False,
                 **kwargs):
        super(RelPartialLearnableDecoderLayer, self).__init__()

        self.dec_attn = RelPartialLearnableMultiHeadAttn(n_head, d_model,
                                                         d_head, dropout,
                                                         **kwargs)
        self.pos_ff = PositionwiseFF(d_model, d_inner, dropout, last=last,
                                     pre_lnorm=kwargs.get('pre_lnorm'))

        self.dec_attn.set_o_out(self.pos_ff.CoreNet[0])
        self.last = last

    # layers should contain lin2's from all PositionwiseFF and o_nets from all multiheadattn in the model (maybe except ours?)
    def pair_up(self, gates):
        self.dec_attn.set_paired_to_o(gates)
        if not self.last:
            self.pos_ff.set_paired_out(gates)

    def set_ff_out(self, layer):
        if not self.last:
            self.pos_ff.set_output_gate_layer(layer)

    def forward(self, dec_inp, r, r_w_bias, r_r_bias, dec_attn_mask=None, mems=None):

        output = self.dec_attn(dec_inp, r, r_w_bias, r_r_bias,
                               attn_mask=dec_attn_mask,
                               mems=mems)
        output = self.pos_ff(output)

        return output


class AdaptiveEmbedding(nn.Module):
    def __init__(self, n_token, d_embed, d_proj, cutoffs, div_val=1,
                 sample_softmax=False):
        super(AdaptiveEmbedding, self).__init__()

        self.n_token = n_token
        self.d_embed = d_embed

        self.cutoffs = cutoffs + [n_token]
        self.div_val = div_val
        self.d_proj = d_proj

        self.emb_scale = d_proj ** 0.5

        self.cutoff_ends = [0] + self.cutoffs

        self.emb_layers = nn.ModuleList()
        self.emb_projs = nn.ParameterList()
        if div_val == 1:
            self.emb_layers.append(
                nn.Embedding(n_token, d_embed, sparse=(sample_softmax > 0))
            )
            if d_proj != d_embed:
                self.emb_projs.append(nn.Parameter(torch.Tensor(d_proj, d_embed)))
        else:
            for i in range(len(self.cutoffs)):
                l_idx, r_idx = self.cutoff_ends[i], self.cutoff_ends[i+1]
                d_emb_i = d_embed // (div_val ** i)
                self.emb_layers.append(nn.Embedding(r_idx-l_idx, d_emb_i))
                self.emb_projs.append(nn.Parameter(torch.Tensor(d_proj, d_emb_i)))

    def forward(self, inp):
        if self.div_val == 1:
            embed = self.emb_layers[0](inp)
            if self.d_proj != self.d_embed:
                embed = F.linear(embed, self.emb_projs[0])
        else:
            param = next(self.parameters())
            inp_flat = inp.view(-1)
            emb_flat = torch.zeros([inp_flat.size(0), self.d_proj],
                                   dtype=param.dtype, device=param.device)
            for i in range(len(self.cutoffs)):
                l_idx, r_idx = self.cutoff_ends[i], self.cutoff_ends[i + 1]

                mask_i = (inp_flat >= l_idx) & (inp_flat < r_idx)
                indices_i = mask_i.nonzero().squeeze()

                if indices_i.numel() == 0:
                    continue

                inp_i = inp_flat.index_select(0, indices_i) - l_idx
                emb_i = self.emb_layers[i](inp_i)
                emb_i = F.linear(emb_i, self.emb_projs[i])

                emb_flat.index_copy_(0, indices_i, emb_i)

            embed = emb_flat.view(*inp.size(), self.d_proj)

        embed.mul_(self.emb_scale)

        return embed


class MemTransformerLM(nn.Module):
    def __init__(self, n_token, n_layer, n_head, d_model, d_head, d_inner,
                 dropout, dropatt, dtype, tie_weight=True, d_embed=None,
                 div_val=1, tie_projs=[False], pre_lnorm=False,
                 tgt_len=None, ext_len=None, mem_len=None,
                 cutoffs=[], adapt_inp=False,
                 same_length=False, attn_type=0, clamp_len=-1,
                 sample_softmax=-1):
        super(MemTransformerLM, self).__init__()
        self.n_token = n_token

        d_embed = d_model if d_embed is None else d_embed
        self.d_embed = d_embed
        self.d_model = d_model
        self.n_head = n_head
        self.d_head = d_head

        self.word_emb = AdaptiveEmbedding(n_token, d_embed, d_model, cutoffs,
                                          div_val=div_val)

        self.drop = nn.Dropout(dropout)

        self.tie_weight = tie_weight
        self.tie_projs = tie_projs
        self.div_val = div_val

        self.n_layer = n_layer

        self.tgt_len = tgt_len
        self.mem_len = mem_len
        self.ext_len = ext_len
        self.max_klen = tgt_len + ext_len + mem_len

        self.attn_type = attn_type

        self.layers = nn.ModuleList()

        o_nets_and_ffs = []

        # the default attention
        # assumes that sample_softmax <= 0 (otherwise we can prune last decoder's positionwiseFF as well, because it's not the last layer)
        if attn_type == 0:
            for i in range(n_layer):
                decoder = RelPartialLearnableDecoderLayer(
                        n_head, d_model, d_head, d_inner, dropout,
                        tgt_len=tgt_len, ext_len=ext_len, mem_len=mem_len,
                        dropatt=dropatt, last=(i==n_layer - 1), first=(i==0), pre_lnorm=pre_lnorm)
                
                o_nets_and_ffs.append(decoder.dec_attn.o_net[1])
                if i != n_layer - 1:
                    o_nets_and_ffs.append(decoder.pos_ff.CoreNet[5])
                    # last ff's lin2 won't be pruned

                self.layers.append(decoder)
        # learnable embeddings
        elif attn_type == 1:
            for i in range(n_layer):
                self.layers.append(
                    RelLearnableDecoderLayer(
                        n_head, d_model, d_head, d_inner, dropout,
                        tgt_len=tgt_len, ext_len=ext_len, mem_len=mem_len,
                        dropatt=dropatt, pre_lnorm=pre_lnorm)
                )
        # absolute embeddings
        elif attn_type in [2, 3]:
            for i in range(n_layer):
                self.layers.append(
                    DecoderLayer(
                        n_head, d_model, d_head, d_inner, dropout,
                        dropatt=dropatt, pre_lnorm=pre_lnorm)
                )

        self.sample_softmax = sample_softmax
        # use sampled softmax
        if sample_softmax > 0:
            self.out_layer = nn.Linear(d_model, n_token)
            self.tie_weight = tie_weight
            self.sampler = LogUniformSampler(n_token, sample_softmax)

        # use adaptive softmax (including standard softmax)
        else:
            if tie_weight:
                emb_layers = [i.weight for i in self.word_emb.emb_layers]
            else:
                emb_layers = None

            emb_projs = self.word_emb.emb_projs

            self.crit = ProjectedAdaptiveLogSoftmax(n_token, d_embed, d_model,
                                                    cutoffs, div_val=div_val,
                                                    tie_projs=tie_projs,
                                                    out_projs=emb_projs,
                                                    out_layers_weights=emb_layers)


        for i, decoder in enumerate(self.layers):
            if i != n_layer - 1:
                decoder.set_ff_out(self.layers[i + 1].dec_attn.qkv_net[0])
        self.layers[n_layer-2].pair_up(o_nets_and_ffs)

        self.same_length = same_length
        self.clamp_len = clamp_len

        self._create_params()

    def backward_compatible(self):
        self.sample_softmax = -1

    def _create_params(self):
        # default attention
        if self.attn_type == 0:
            self.pos_emb = PositionalEmbedding(self.d_model)
            self.r_w_bias = nn.Parameter(torch.Tensor(self.n_head, self.d_head))
            self.r_r_bias = nn.Parameter(torch.Tensor(self.n_head, self.d_head))
        # learnable
        elif self.attn_type == 1:
            self.r_emb = nn.Parameter(torch.Tensor(
                    self.n_layer, self.max_klen, self.n_head, self.d_head))
            self.r_w_bias = nn.Parameter(torch.Tensor(
                    self.n_layer, self.n_head, self.d_head))
            self.r_bias = nn.Parameter(torch.Tensor(
                    self.n_layer, self.max_klen, self.n_head))
        # absolute standard
        elif self.attn_type == 2:
            self.pos_emb = PositionalEmbedding(self.d_model)
        # absolute deeper SA
        elif self.attn_type == 3:
            self.r_emb = nn.Parameter(torch.Tensor(
                    self.n_layer, self.max_klen, self.d_model))

    def reset_length(self, tgt_len, ext_len, mem_len):
        self.tgt_len = tgt_len
        self.mem_len = mem_len
        self.ext_len = ext_len

    def init_mems(self):
        if self.mem_len > 0:
            mems = []
            param = next(self.parameters())
            for i in range(self.n_layer+1):
                empty = torch.empty(0, dtype=param.dtype, device=param.device)
                mems.append(empty)

            return mems
        else:
            return None

    def _update_mems(self, hids, mems, qlen, mlen):
        # does not deal with None
        if mems is None:
            return None

        # mems is not None
        assert len(hids) == len(mems), 'len(hids) != len(mems)'

        # There are `mlen + qlen` steps that can be cached into mems
        # For the next step, the last `ext_len` of the `qlen` tokens
        # will be used as the extended context. Hence, we only cache
        # the tokens from `mlen + qlen - self.ext_len - self.mem_len`
        # to `mlen + qlen - self.ext_len`.
        with torch.no_grad():
            new_mems = []
            end_idx = mlen + max(0, qlen - 0 - self.ext_len)
            beg_idx = max(0, end_idx - self.mem_len)
            for i in range(len(hids)):

                cat = torch.cat([mems[i], hids[i]], dim=0)
                new_mems.append(cat[beg_idx:end_idx].detach())

        return new_mems

    def _forward(self, dec_inp, mems=None):
        qlen, bsz = dec_inp.size()

        word_emb = self.word_emb(dec_inp)

        mlen = mems[0].size(0) if mems is not None else 0
        klen = mlen + qlen
        if self.same_length:
            all_ones = word_emb.new_ones(qlen, klen)
            mask_len = klen - self.mem_len - 1
            if mask_len > 0:
                mask_shift_len = qlen - mask_len
            else:
                mask_shift_len = qlen
            dec_attn_mask = (torch.triu(all_ones, 1+mlen)
                             + torch.tril(all_ones, -mask_shift_len)).bool()
        else:
            dec_attn_mask = torch.triu(
                word_emb.new_ones(qlen, klen), diagonal=1+mlen).bool()

        hids = []
        # default
        if self.attn_type == 0:
            pos_seq = torch.arange(klen-1, -1, -1.0, device=word_emb.device,
                                   dtype=word_emb.dtype)
            if self.clamp_len > 0:
                pos_seq.clamp_(max=self.clamp_len)
            pos_emb = self.pos_emb(pos_seq)

            core_out = self.drop(word_emb)
            pos_emb = self.drop(pos_emb)

            hids.append(core_out)
            for i, layer in enumerate(self.layers):
                mems_i = None if mems is None else mems[i]
                core_out = layer(core_out, pos_emb, self.r_w_bias,
                                 self.r_r_bias, dec_attn_mask=dec_attn_mask,
                                 mems=mems_i)
                hids.append(core_out)
        # learnable
        elif self.attn_type == 1:
            core_out = self.drop(word_emb)
            hids.append(core_out)
            for i, layer in enumerate(self.layers):
                if self.clamp_len > 0:
                    r_emb = self.r_emb[i][-self.clamp_len:]
                    r_bias = self.r_bias[i][-self.clamp_len:]
                else:
                    r_emb, r_bias = self.r_emb[i], self.r_bias[i]

                mems_i = None if mems is None else mems[i]
                core_out = layer(core_out, r_emb, self.r_w_bias[i],
                                 r_bias, dec_attn_mask=dec_attn_mask, mems=mems_i)
                hids.append(core_out)
        # absolute
        elif self.attn_type == 2:
            pos_seq = torch.arange(klen - 1, -1, -1.0, device=word_emb.device,
                                   dtype=word_emb.dtype)
            if self.clamp_len > 0:
                pos_seq.clamp_(max=self.clamp_len)
            pos_emb = self.pos_emb(pos_seq)

            core_out = self.drop(word_emb + pos_emb[-qlen:])

            hids.append(core_out)
            for i, layer in enumerate(self.layers):
                mems_i = None if mems is None else mems[i]
                if mems_i is not None and len(mems_i) and i == 0:
                    mems_i += pos_emb[:mlen]
                core_out = layer(core_out, dec_attn_mask=dec_attn_mask,
                                 mems=mems_i)
                hids.append(core_out)
        elif self.attn_type == 3:
            core_out = self.drop(word_emb)

            hids.append(core_out)
            for i, layer in enumerate(self.layers):
                mems_i = None if mems is None else mems[i]
                if mems_i is not None and len(mems_i) and mlen > 0:
                    cur_emb = self.r_emb[i][:-qlen]
                    cur_size = cur_emb.size(0)
                    if cur_size < mlen:
                        cur_emb_pad = cur_emb[0:1].expand(mlen-cur_size, -1, -1)
                        cur_emb = torch.cat([cur_emb_pad, cur_emb], 0)
                    else:
                        cur_emb = cur_emb[-mlen:]
                    mems_i += cur_emb.view(mlen, 1, -1)
                core_out += self.r_emb[i][-qlen:].view(qlen, 1, -1)

                core_out = layer(core_out, dec_attn_mask=dec_attn_mask,
                                 mems=mems_i)
                hids.append(core_out)

        core_out = self.drop(core_out)

        new_mems = self._update_mems(hids, mems, qlen, mlen)

        return core_out, new_mems

    def forward(self, data, target, mems):
        # nn.DataParallel does not allow size(0) tensors to be broadcasted.
        # So, have to initialize size(0) mems inside the model forward.
        # Moreover, have to return new_mems to allow nn.DataParallel to piece
        # them together.
        if mems is None:
            mems = self.init_mems()

        tgt_len = target.size(0)
        hidden, new_mems = self._forward(data, mems=mems)

        pred_hid = hidden[-tgt_len:]
        if self.sample_softmax > 0 and self.training:
            assert self.tie_weight
            logit = sample_logits(self.word_emb, self.out_layer.bias, target,
                                  pred_hid, self.sampler)
            loss = -F.log_softmax(logit, -1)[:, :, 0]
        else:
            loss = self.crit(pred_hid.view(-1, pred_hid.size(-1)), target.view(-1))
            loss = loss.view(tgt_len, -1)

        return (loss, new_mems)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='unit test')

    parser.add_argument('--n_layer', type=int, default=4, help='')
    parser.add_argument('--n_rel_layer', type=int, default=4, help='')
    parser.add_argument('--n_head', type=int, default=2, help='')
    parser.add_argument('--d_head', type=int, default=2, help='')
    parser.add_argument('--d_model', type=int, default=200, help='')
    parser.add_argument('--d_embed', type=int, default=200, help='')
    parser.add_argument('--d_inner', type=int, default=200, help='')
    parser.add_argument('--dropout', type=float, default=0.0, help='')
    parser.add_argument('--cuda', action='store_true', help='')
    parser.add_argument('--seed', type=int, default=1111, help='')
    parser.add_argument('--multi_gpu', action='store_true', help='')

    args = parser.parse_args()

    device = torch.device("cuda" if args.cuda else "cpu")

    B = 4
    tgt_len, mem_len, ext_len = 36, 36, 0
    data_len = tgt_len * 20
    args.n_token = 10000

    import data_utils

    data = torch.LongTensor(data_len*B).random_(0, args.n_token).to(device)
    diter = data_utils.LMOrderedIterator(data, B, tgt_len, device=device, ext_len=ext_len)

    cutoffs = [args.n_token // 2]
    tie_projs = [False] + [True] * len(cutoffs)

    for div_val in [1, 2]:
        for d_embed in [200, 100]:
            model = MemTransformerLM(args.n_token, args.n_layer, args.n_head,
                                     args.d_model, args.d_head, args.d_inner,
                                     args.dropout, dropatt=args.dropout,
                                     tie_weight=True, d_embed=d_embed,
                                     div_val=div_val, tie_projs=tie_projs,
                                     pre_lnorm=True, tgt_len=tgt_len,
                                     ext_len=ext_len, mem_len=mem_len,
                                     cutoffs=cutoffs, attn_type=0,
                                     dtype=None).to(device)

            print(sum(p.numel() for p in model.parameters()))

            mems = None
            for idx, (inp, tgt, seqlen, _) in enumerate(diter):
                print('batch {}'.format(idx))
                _, mems = model(inp, tgt, mems)
