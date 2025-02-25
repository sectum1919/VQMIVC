import torch
import torch.nn as nn
import torch.nn.functional as F
from src.espnet.nets.pytorch_backend.transformer.encoder import Encoder as Conformer
from src.espnet.nets.pytorch_backend.nets_utils import make_non_pad_mask
# import numpy as np
'''
reference from: https://github.com/auspicious3000/autovc/blob/master/model_vc.py
'''

class LinearNorm(torch.nn.Module):
    def __init__(self, in_dim, out_dim, bias=True, w_init_gain='linear'):
        super(LinearNorm, self).__init__()
        self.linear_layer = torch.nn.Linear(in_dim, out_dim, bias=bias)

        torch.nn.init.xavier_uniform_(
            self.linear_layer.weight,
            gain=torch.nn.init.calculate_gain(w_init_gain))

    def forward(self, x):
        return self.linear_layer(x)


class ConvNorm(torch.nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=1, stride=1,
                 padding=None, dilation=1, bias=True, w_init_gain='linear'):
        super(ConvNorm, self).__init__()
        if padding is None:
            assert(kernel_size % 2 == 1)
            padding = int(dilation * (kernel_size - 1) / 2)

        self.conv = torch.nn.Conv1d(in_channels, out_channels,
                                    kernel_size=kernel_size, stride=stride,
                                    padding=padding, dilation=dilation,
                                    bias=bias)

        torch.nn.init.xavier_uniform_(
            self.conv.weight, gain=torch.nn.init.calculate_gain(w_init_gain))

    def forward(self, signal):
        conv_signal = self.conv(signal)
        return conv_signal

    
    
class Postnet(nn.Module):
    """Postnet
        - Five 1-d convolution with 512 channels and kernel size 5
    """

    def __init__(self):
        super(Postnet, self).__init__()
        self.convolutions = nn.ModuleList()

        self.convolutions.append(
            nn.Sequential(
                ConvNorm(80, 512,
                         kernel_size=5, stride=1,
                         padding=2,
                         dilation=1, w_init_gain='tanh'),
                nn.BatchNorm1d(512))
        )

        for i in range(1, 5 - 1):
            self.convolutions.append(
                nn.Sequential(
                    ConvNorm(512,
                             512,
                             kernel_size=5, stride=1,
                             padding=2,
                             dilation=1, w_init_gain='tanh'),
                    nn.BatchNorm1d(512))
            )

        self.convolutions.append(
            nn.Sequential(
                ConvNorm(512, 80,
                         kernel_size=5, stride=1,
                         padding=2,
                         dilation=1, w_init_gain='linear'),
                nn.BatchNorm1d(80))
            )

    def forward(self, x):
        for i in range(len(self.convolutions) - 1):
            x = torch.tanh(self.convolutions[i](x))

        x = self.convolutions[-1](x)

        return x    
    
class Decoder_conformer(nn.Module):
    def __init__(self):
        super().__init__()
        self.conformer = Conformer(
            idim=417,
            attention_dim=384,
            attention_heads=2,
            linear_units=1536,
            num_blocks=4,
            input_layer='linear',
            dropout_rate=0.1,
            positional_dropout_rate=0.1,
            attention_dropout_rate=0.1,
            encoder_attn_layer_type='rel_mha',
            macaron_style=True,
            use_cnn_module=True,
            cnn_module_kernel=31,
            zero_triu=False,
            a_upsample_ratio=1,
            relu_type='swish',
        )
        self.linear = nn.Linear(in_features=384, out_features=80)
        
        
    def forward(self, x):
        # import pdb
        # pdb.set_trace()
        B, T, C = x.shape
        x_mask = make_non_pad_mask(lengths=[T for _ in x]).to(x.device).unsqueeze(-2)
        embedding, _ = self.conformer(x, x_mask)
        mel_output = self.linear(embedding)
        return mel_output
    

class Decoder(nn.Module):
    """Decoder module:
    """
    def __init__(self, dim_neck=64, dim_lf0=1, dim_emb=256, dim_pre=512):
        super(Decoder, self).__init__()
        
        self.lstm1 = nn.LSTM(dim_neck+dim_emb+dim_lf0, dim_pre, 1, batch_first=True)
        
        convolutions = []
        for i in range(3):
            conv_layer = nn.Sequential(
                ConvNorm(dim_pre,
                         dim_pre,
                         kernel_size=5, stride=1,
                         padding=2,
                         dilation=1, w_init_gain='relu'),
                nn.BatchNorm1d(dim_pre))
            convolutions.append(conv_layer)
        self.convolutions = nn.ModuleList(convolutions)
        
        self.lstm2 = nn.LSTM(dim_pre, 1024, 2, batch_first=True)
        
        self.linear_projection = LinearNorm(1024, 80)

    def forward(self, x):
        
        #self.lstm1.flatten_parameters()
        x, _ = self.lstm1(x)
        x = x.transpose(1, 2)
        
        for conv in self.convolutions:
            x = F.relu(conv(x))
        x = x.transpose(1, 2)
        
        outputs, _ = self.lstm2(x)
        
        decoder_output = self.linear_projection(outputs)

        return decoder_output   
    
    
    
        
class Decoder_ac(nn.Module):
    """Decoder_ac network."""
    def __init__(self, dim_neck=64, dim_lf0=1, dim_emb=256, dim_pre=512, use_l1_loss=False):
        super(Decoder_ac, self).__init__()
        self.use_l1_loss = use_l1_loss
        # self.encoder = Encoder(dim_neck, dim_emb, freq)
        # self.decoder = Decoder(dim_neck, dim_lf0, dim_emb, dim_pre)
        self.decoder = Decoder_conformer()
        self.postnet = Postnet()

    def forward(self, z, lf0_embs, spk_embs, mel_target=None):
        # import pdb
        # pdb.set_trace()
        # z.shape (B, C, T/2) == (256, 160, T/2)
        # spk_embs.shape (B, C) == (256, 256)
        z = F.interpolate(z.transpose(1, 2), scale_factor=2) # (B, T/2, C) -> (B, C, T/2) -> (B, C, T)
        z = z.transpose(1, 2) # (B, 160=C, T/2) -> (B, T, 160=C)
        spk_embs_exp = spk_embs.unsqueeze(1).expand(-1,z.shape[1],-1)
        lf0_embs = lf0_embs[:,:z.shape[1],:]
        # print(z.shape, lf0_embs.shape)
        x = torch.cat([z, lf0_embs, spk_embs_exp], dim=-1)
        
        mel_outputs = self.decoder(x)
                
        mel_outputs_postnet = self.postnet(mel_outputs.transpose(2,1))
        mel_outputs_postnet = mel_outputs + mel_outputs_postnet.transpose(2,1)
        # print('mel_outputs.shape:', mel_outputs_postnet.shape)
        if mel_target is None:
            return mel_outputs_postnet
        else:
            # mel_target = mel_target[:,1:-1,:]
            loss = F.mse_loss(mel_outputs, mel_target) + \
                F.mse_loss(mel_outputs_postnet, mel_target)
            if self.use_l1_loss:
                loss = loss + F.l1_loss(mel_outputs, mel_target) + \
                    F.l1_loss(mel_outputs_postnet, mel_target)
            return loss, mel_outputs_postnet


