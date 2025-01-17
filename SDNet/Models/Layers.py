# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import os
import sys
import random
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torch.nn.init as init
from torch.nn.parameter import Parameter
from torch.nn.utils.rnn import pad_packed_sequence as unpack
from torch.nn.utils.rnn import pack_padded_sequence as pack
import copy
import math
from torch.autograd import Variable

def set_dropout_prob(p):
    global dropout_p
    dropout_p = p

def set_seq_dropout(option): # option = True or False
    global do_seq_dropout
    do_seq_dropout = option

def seq_dropout(x, p=0, training=False):
    """
    x: batch * len * input_size
    """
    if training == False or p == 0:
        return x
    dropout_mask = Variable(1.0 / (1-p) * torch.bernoulli((1-p) * (x.data.new(x.size(0), x.size(2)).zero_() + 1)), requires_grad=False)
    return dropout_mask.unsqueeze(1).expand_as(x) * x    

def dropout(x, p=0, training=False):
    """
    x: (batch * len * input_size) or (any other shape)
    """
    if do_seq_dropout and len(x.size()) == 3: # if x is (batch * len * input_size)
        return seq_dropout(x, p=p, training=training)
    else:
        return F.dropout(x, p=p, training=training)


class CNN(nn.Module):
    def __init__(self, input_size, window_size, output_size):
        super(CNN, self).__init__()
        if window_size % 2 != 1:
            raise Exception("window size must be an odd number")
        padding_size = int((window_size - 1) / 2)
        self._output_size = output_size
        self.cnn = nn.Conv2d(1, output_size, (window_size, input_size), padding = (padding_size, 0), bias = False)
        init.xavier_uniform(self.cnn.weight)

    @property
    def output_size(self):
        return self._output_size

    '''
     (item, subitem) can be (word, characters), or (sentence, words)
     x: num_items x max_subitem_size x input_size
     x_mask: num_items x max_subitem_size (not used but put here to align with RNN format)
     return num_items x max_subitem_size x output_size
    '''
    def forward(self, x, x_mask):
        '''
         x_unsqueeze: num_items x 1 x max_subitem_size x input_size  
         x_conv: num_items x output_size x max_subitem_size
         x_output: num_items x max_subitem_size x output_size
        '''
        x = F.dropout(x, p = dropout_p, training = self.training)
        x_unsqueeze = x.unsqueeze(1) 
        x_conv = F.tanh(self.cnn(x_unsqueeze)).squeeze(3)
        x_output = torch.transpose(x_conv, 1, 2)
        return x_output

class MaxPooling(nn.Module):
    def __init__(self):
        super(MaxPooling, self).__init__()
        self.MIN = -1e6

    '''
     (item, subitem) can be (word, characters), or (sentence, words)
     x: num_items x max_subitem_size x input_size
     x_mask: num_items x max_subitem_size
     return num_items x input_size
    '''
    def forward(self, x, x_mask):
        '''
         x_output: num_items x input_size x 1 --> num_items x input_size
        '''
        empty_mask = x_mask.eq(0).unsqueeze(2).expand_as(x)
        x_now = x.clone()
        x_now.data.masked_fill_(empty_mask.data, self.MIN)
        x_output = x_now.max(1)[0]
        x_output.data.masked_fill_(x_output.data.eq(self.MIN), 0)

        return x_output

class AveragePooling(nn.Module):
    def __init__(self):
        super(AveragePooling, self).__init__()

    '''
     (item, subitem) can be (word, characters), or (sentence, words)
     x: num_items x max_subitem_size x input_size
     x_mask: num_items x max_subitem_size
     return num_items x input_size
    '''
    def forward(self, x, x_mask):
        '''
         x_output: num_items x input_size x 1 --> num_items x input_size
        '''
        x_now = x.clone()
        empty_mask = x_mask.eq(0).unsqueeze(2).expand_as(x_now)
        x_now.data.masked_fill_(empty_mask.data, 0)
        x_sum = torch.sum(x_now, 1);
        # x_sum: num_items x input_size

        x_num = torch.sum(x_mask.eq(1).float(), 1).unsqueeze(1).expand_as(x_sum);
        # x_num: num_items x input_size

        x_num = torch.clamp(x_num, min = 1)

        return x_sum / x_num;

# 1 layer that's normal
# 1 layer with window size 3
# 1 layer with window size 7
# 1 layer with diluted window size 5

class LayeredCNN(nn.Module):
    def __init__(self, input_size, output_size, window_sizes, dilations, max_pool_kernel = (1,4), cnn_type = nn.Conv2d):
        super(LayeredCNN, self).__init__()        
        self._output_size = output_size
        self.num_convolutions = len(window_sizes)

        assert len(window_sizes) == len(dilations)

        def init_weights(m):
            if type(m) == cnn_type:
                init.xavier_uniform(m.weight)

        self.cnns = nn.ModuleList()
        for i in range (self.num_convolutions):
            padding_size = int((dilations[i] * (window_sizes[i] - 1)) / 2)
            self.cnns.append(nn.Sequential(
                cnn_type(1, output_size, window_sizes[i], padding = padding_size, dilation=dilations[i]),
                nn.Tanh(),
            ))
        self.mp = nn.MaxPool2d(max_pool_kernel)

        # self.cnns = nn.ModuleList()
        # for i in range (len(window_sizes)):


        #     padding_size = int((dilations[i] * (window_sizes[i] - 1)) / 2)
        #     cnn = cnn_type(1, output_size, window_size[i], padding = padding_size, dilation=dilations[i])
        #     init.xavier_uniform(cnn.weight)
        #     self.cnns.append(cnn)



    @property
    def output_size(self):
        return self._output_size

    '''
     (item, subitem) can be (word, characters), or (sentence, words)
     x: num_items x max_subitem_size x input_size
     x_mask: num_items x max_subitem_size (not used but put here to align with RNN format)
     return num_items x max_subitem_size x output_size
    '''
    def forward(self, x):
        '''
         x_unsqueeze: num_ite ms x 1 x max_subitem_size x input_size  
         x_conv: num_items x output_size x max_subitem_size
         x_output: num_items x max_subitem_size x output_size
        '''

        convs = [x.unsqueeze(1)]
        for i in range (self.num_convolutions):
            x_unsqueeze = x.unsqueeze(1)
            convs.append(self.cnns[i](x_unsqueeze))
        stacked_cnns = torch.cat(convs, dim = 1)
        to_return = torch.mean(stacked_cnns, 1)
        return to_return.squeeze(1)


        # hiddens = [x]
        # for i in range (self.num_layers):
        #     curr_x = hiddens[-1]
        #     x_dropout = F.dropout(curr_x, p = dropout_p, training = self.training)
        #     x_unsqueeze = x_dropout.unsqueeze(1)
        #     if i == 1 and x_additional is not None:
        #         x_unsqueeze = torch.cat((x_unsqueeze, x_additional), 3)
        #     x_conv = F.tanh(self.cnns[i](x_unsqueeze)).squeeze(3)
        #     hiddens.append(torch.transpose(x_conv, 1, 2))
        # output = hiddens[-1]

        # if return_list:
        #     return output, hiddens[1:]
        # else:
        #     return output

# #input_size, window_size, output_size
class StackedCNN(nn.Module):
    def __init__(self, input_size, output_size, window_size, num_layers=2, add_feat=0, cnn_type = nn.Conv2d):
        super(StackedCNN, self).__init__()
        self.num_layers = num_layers
        padding_size = int((window_size - 1) / 2)
        self._output_size = output_size
        # self.cnn = nn.Conv2d(1, int(output_size/2), (window_size, input_size), padding = (padding_size, 0))
        # intermediate_feature_size = int(output_size/2) + add_feat
        # print ('derek' + str(intermediate_feature_size))
        # self.cnn2 = nn.Conv2d(1, output_size, (window_size, intermediate_feature_size), padding = (padding_size, 0))
        # init.xavier_uniform(self.cnn.weight)
        # init.xavier_uniform(self.cnn2.weight)

        self.cnns = nn.ModuleList()
        intermediate_feature_size = input_size
        for i in range (num_layers):
            cnn = cnn_type(1, output_size, (window_size, intermediate_feature_size), padding = (padding_size, 0))
            init.xavier_uniform(cnn.weight)
            intermediate_feature_size = output_size + add_feat if i == 1 else output_size
            self.cnns.append(cnn)



        # for i in range(num_layers):
        #     in_size = input_size if i == 0 else (self.bidir_coef * hidden_size + add_feat if i== 1 else self.bidir_coef * hidden_size)
        #     rnn = rnn_type(in_size, hidden_size, num_layers = 1, bidirectional = bidirectional, batch_first = True)
        #     self.rnns.append(rnn)

    @property
    def output_size(self):
        return self._output_size

    '''
     (item, subitem) can be (word, characters), or (sentence, words)
     x: num_items x max_subitem_size x input_size
     x_mask: num_items x max_subitem_size (not used but put here to align with RNN format)
     return num_items x max_subitem_size x output_size
    '''
    def forward(self, x, x_mask, return_list=False, x_additional = None):
        '''
         x_unsqueeze: num_items x 1 x max_subitem_size x input_size  
         x_conv: num_items x output_size x max_subitem_size
         x_output: num_items x max_subitem_size x output_size
        '''
        hiddens = [x]
        for i in range (self.num_layers):
            curr_x = hiddens[-1]
            x_dropout = F.dropout(curr_x, p = dropout_p, training = self.training)
            x_unsqueeze = x_dropout.unsqueeze(1)
            if i == 1 and x_additional is not None:
                x_unsqueeze = torch.cat((x_unsqueeze, x_additional), 3)
            x_conv = F.tanh(self.cnns[i](x_unsqueeze)).squeeze(3)
            hiddens.append(torch.transpose(x_conv, 1, 2))
        output = hiddens[-1]

        if return_list:
            return output, hiddens[1:]
        else:
            return output

class StackedBRNN(nn.Module):
    def __init__(self, input_size, hidden_size, num_layers, rnn_type = nn.LSTM, concat_layers = False, bidirectional = True, add_feat=0):
        super(StackedBRNN, self).__init__()
        self.bidir_coef = 2 if bidirectional else 1
        self.num_layers = num_layers
        self.concat_layers = concat_layers
        self.hidden_size = hidden_size
        self.rnns = nn.ModuleList()
        for i in range(num_layers):
            in_size = input_size if i == 0 else (self.bidir_coef * hidden_size + add_feat if i== 1 else self.bidir_coef * hidden_size)
            rnn = rnn_type(in_size, hidden_size, num_layers = 1, bidirectional = bidirectional, batch_first = True)
            self.rnns.append(rnn)
    
    @property
    def output_size(self):
        if self.concat_layers:
            return self.num_layers * self.bidir_coef * self.hidden_size
        else:
            return self.bidir_coef * self.hidden_size

    """
       Multi-layer bi-RNN
              
       Arguments:
           x (Float Tensor): a Float Tensor of size (batch * wordnum * input_dim).
           x_mask (Byte Tensor): a Byte Tensor of mask for the input tensor (batch * wordnum).
           x_additional (Byte Tensor): a Byte Tensor of mask for the additional input tensor (batch * wordnum * additional_dim).
           x_out (Float Tensor): a Float Tensor of size (batch * wordnum * output_size).
    """
    def forward(self, x, x_mask, return_list=False, x_additional = None):
        hiddens = [x]
        for i in range(self.num_layers):
            rnn_input = hiddens[-1]
            
            if i == 1 and x_additional is not None:
                rnn_input = torch.cat((rnn_input, x_additional), 2)

            if dropout_p > 0:
                rnn_input = dropout(rnn_input, p=dropout_p, training = self.training)

            rnn_output = self.rnns[i](rnn_input)[0]
            hiddens.append(rnn_output)

        if self.concat_layers:
            output = torch.cat(hiddens[1:], 2)
        else:
            output = hiddens[-1]

        if return_list:
            return output, hiddens[1:]
        else:
            return output

class AttentionScore(nn.Module):
    """
    correlation_func = 1, sij = x1^Tx2
    correlation_func = 2, sij = (Wx1)D(Wx2)
    correlation_func = 3, sij = Relu(Wx1)DRelu(Wx2)
    correlation_func = 4, sij = x1^TWx2
    correlation_func = 5, sij = Relu(Wx1)DRelu(Wx2)
    """
    def __init__(self, input_size, hidden_size, correlation_func = 1, do_similarity = False):
        super(AttentionScore, self).__init__()
        self.correlation_func = correlation_func
        self.hidden_size = hidden_size
        
        if correlation_func == 2 or correlation_func == 3:
            self.linear = nn.Linear(input_size, hidden_size, bias = False)
            if do_similarity:
                self.diagonal = Parameter(torch.ones(1, 1, 1) / (hidden_size ** 0.5), requires_grad = False)
            else:
                self.diagonal = Parameter(torch.ones(1, 1, hidden_size), requires_grad = True)

        if correlation_func == 4:
            self.linear = nn.Linear(input_size, input_size, bias=False)

        if correlation_func == 5:
            self.linear = nn.Linear(input_size, hidden_size, bias = False)    
        
    def forward(self, x1, x2):
        '''
        Input:
        x1: batch x word_num1 x dim
        x2: batch x word_num2 x dim
        Output:
        scores: batch x word_num1 x word_num2
        '''
        x1 = dropout(x1, p = dropout_p, training = self.training)
        x2 = dropout(x2, p = dropout_p, training = self.training)

        x1_rep = x1
        x2_rep = x2
        batch = x1_rep.size(0)
        word_num1 = x1_rep.size(1)
        word_num2 = x2_rep.size(1)
        dim = x1_rep.size(2)
        if self.correlation_func == 2 or self.correlation_func == 3:
            x1_rep = self.linear(x1_rep.contiguous().view(-1, dim)).view(batch, word_num1, self.hidden_size)  # Wx1
            x2_rep = self.linear(x2_rep.contiguous().view(-1, dim)).view(batch, word_num2, self.hidden_size)  # Wx2
            if self.correlation_func == 3:
                x1_rep = F.relu(x1_rep)
                x2_rep = F.relu(x2_rep)
            x1_rep = x1_rep * self.diagonal.expand_as(x1_rep) 
            # x1_rep is (Wx1)D or Relu(Wx1)D
            # x1_rep: batch x word_num1 x dim (corr=1) or hidden_size (corr=2,3)

        if self.correlation_func == 4:
            x2_rep = self.linear(x2_rep.contiguous().view(-1, dim)).view(batch, word_num2, dim)  # Wx2

        if self.correlation_func == 5:
            x1_rep = self.linear(x1_rep.contiguous().view(-1, dim)).view(batch, word_num1, self.hidden_size)  # Wx1
            x2_rep = self.linear(x2_rep.contiguous().view(-1, dim)).view(batch, word_num2, self.hidden_size)  # Wx2
            x1_rep = F.relu(x1_rep)
            x2_rep = F.relu(x2_rep)    
            
        scores = x1_rep.bmm(x2_rep.transpose(1, 2))
        return scores

class Attention(nn.Module):
    def __init__(self, input_size, hidden_size, correlation_func = 1, do_similarity = False):
        super(Attention, self).__init__()
        self.scoring = AttentionScore(input_size, hidden_size, correlation_func, do_similarity)

    def forward(self, x1, x2, x2_mask, x3 = None, drop_diagonal=False):
        '''
        For each word in x1, get its attended linear combination of x3 (if none, x2), 
         using scores calculated between x1 and x2.
        Input:
         x1: batch x word_num1 x dim
         x2: batch x word_num2 x dim
         x2_mask: batch x word_num2
         x3 (if not None) : batch x word_num2 x dim_3
        Output:
         attended: batch x word_num1 x dim_3
        '''
        batch = x1.size(0)
        word_num1 = x1.size(1)
        word_num2 = x2.size(1)

        if x3 is None:
            x3 = x2

        scores = self.scoring(x1, x2)

        # scores: batch x word_num1 x word_num2
        empty_mask = x2_mask.eq(0).unsqueeze(1).expand_as(scores)
        scores.data.masked_fill_(empty_mask.data, -float('inf'))

        if drop_diagonal:
            assert(scores.size(1) == scores.size(2))
            diag_mask = torch.diag(scores.data.new(scores.size(1)).zero_() + 1).byte().unsqueeze(0).expand_as(scores)
            scores.data.masked_fill_(diag_mask, -float('inf'))

        # softmax
        alpha_flat = F.softmax(scores.view(-1, x2.size(1)), dim = 1)
        alpha = alpha_flat.view(-1, x1.size(1), x2.size(1))
        # alpha: batch x word_num1 x word_num2

        attended = alpha.bmm(x3)
        # attended: batch x word_num1 x dim_3

        return attended

def RNN_from_opt(input_size_, hidden_size_, num_layers=1, concat_rnn=False, add_feat=0, bidirectional=True, rnn_type=nn.LSTM):
    new_rnn = StackedBRNN(
        input_size=input_size_,
        hidden_size=hidden_size_,
        num_layers=num_layers,
        rnn_type=rnn_type,
        concat_layers=concat_rnn,
        bidirectional=bidirectional,
        add_feat=add_feat
    )

    output_size = hidden_size_
    if bidirectional:
        output_size *= 2
    if concat_rnn:
        output_size *= num_layers

    return new_rnn, output_size   

def LayeredCNN_from_opt(input_size_, output_size_, window_sizes_, dilations_, max_pool_kernel_):
    new_cnn = LayeredCNN(
        input_size=input_size_, 
        output_size=output_size_,
        window_sizes=window_sizes_,
        dilations=dilations_,
        max_pool_kernel=max_pool_kernel_
    )

    output_size = output_size_

    return new_cnn, output_size  

def CNN_from_opt(input_size_, output_size_, window_size_, num_layers=2, add_feat=0):
    new_cnn = StackedCNN(
        input_size=input_size_, 
        output_size=output_size_,
        window_size=window_size_, 
        num_layers=num_layers,
        add_feat=add_feat,
    )

    output_size = output_size_

    return new_cnn, output_size  

# For summarizing a set of vectors into a single vector
class LinearSelfAttn(nn.Module):
    """Self attention over a sequence:
    * o_i = softmax(Wx_i) for x_i in X.
    """
    def __init__(self, input_size):
        super(LinearSelfAttn, self).__init__()
        self.linear = nn.Linear(input_size, 1)

    def forward(self, x, x_mask):
        """
        x = batch * len * hdim
        x_mask = batch * len
        """
        empty_mask = x_mask.eq(0).expand_as(x_mask)

        x = dropout(x, p=dropout_p, training=self.training)

        x_flat = x.contiguous().view(-1, x.size(-1))
        scores = self.linear(x_flat).view(x.size(0), x.size(1))
        scores.data.masked_fill_(empty_mask.data, -float('inf'))
        alpha = F.softmax(scores, dim = 1)
        return alpha

def generate_mask(new_data, dropout_p=0.0):
    new_data = (1-dropout_p) * (new_data.zero_() + 1)
    for i in range(new_data.size(0)):
        one = random.randint(0, new_data.size(1) - 1)
        new_data[i][one] = 1
    mask = Variable(1.0/(1 - dropout_p) * torch.bernoulli(new_data), requires_grad=False)
    return mask

# Get positional scores and scores for 'yes', 'no', 'unknown' cases
class GetFinalScores(nn.Module):
    def __init__(self, x_size, h_size):
        super(GetFinalScores, self).__init__()
        self.noanswer_linear = nn.Linear(h_size, x_size)
        self.noanswer_w = nn.Linear(x_size, 1, bias=True)
        self.no_linear = nn.Linear(h_size, x_size)
        self.no_w = nn.Linear(x_size, 1, bias=True) 
        self.yes_linear = nn.Linear(h_size, x_size)
        self.yes_w = nn.Linear(x_size, 1, bias=True) 

        self.attn = BilinearSeqAttn(x_size, h_size)
        self.attn2 = BilinearSeqAttn(x_size, h_size)

        self.rnn = nn.GRUCell(x_size, h_size)

    def forward(self, x, h0, x_mask):
        """
        x = batch * len * x_size
        h0 = batch * h_size
        x_mask = batch * len
        """

        score_s = self.attn(x, h0, x_mask)
        # score_s = batch * len

        ptr_net_in = torch.bmm(F.softmax(score_s, dim = 1).unsqueeze(1), x).squeeze(1)
        ptr_net_in = dropout(ptr_net_in, p=dropout_p, training=self.training)
        h0 = dropout(h0, p=dropout_p, training=self.training)
        h1 = self.rnn(ptr_net_in, h0)
        # h1 same size as h0

        score_e = self.attn2(x, h1, x_mask)
        # score_e = batch * len

        score_no = self.get_single_score(x, h0, x_mask, self.no_linear, self.no_w)
        score_yes = self.get_single_score(x, h0, x_mask, self.yes_linear, self.yes_w)
        score_noanswer = self.get_single_score(x, h0, x_mask, self.noanswer_linear, self.noanswer_w)
        return score_s, score_e, score_no, score_yes, score_noanswer
        # return score_s, score_e, score_noanswer
    
    def get_single_score(self, x, h, x_mask, linear, w):
        Wh = linear(h)  #batch * x_size
        xWh = x.bmm(Wh.unsqueeze(2)).squeeze(2) #batch * len

        empty_mask = x_mask.eq(0).expand_as(x_mask)
        xWh.data.masked_fill_(empty_mask.data, -float('inf'))

        attn_x = torch.bmm(F.softmax(xWh, dim = 1).unsqueeze(1), x) # batch * 1 * x_size
        single_score = w(attn_x).squeeze(2) # batch * 1

        return single_score

# For attending the span in document from the query
class BilinearSeqAttn(nn.Module):
    """A bilinear attention layer over a sequence X w.r.t y:
    * o_i = x_i'Wy for x_i in X.
    """
    def __init__(self, x_size, y_size, identity=False):
        super(BilinearSeqAttn, self).__init__()
        if not identity:
            self.linear = nn.Linear(y_size, x_size)
        else:
            self.linear = None          

    def forward(self, x, y, x_mask):
        """
        x = batch * len * h1
        y = batch * h2
        x_mask = batch * len
        """
        empty_mask = x_mask.eq(0).expand_as(x_mask)

        x = dropout(x, p=dropout_p, training=self.training)
        y = dropout(y, p=dropout_p, training=self.training)

        Wy = self.linear(y) if self.linear is not None else y  # batch * h1
        xWy = x.bmm(Wy.unsqueeze(2)).squeeze(2)  # batch * len
        xWy.data.masked_fill_(empty_mask.data, -float('inf'))
        return xWy

# History-of-Word Multi-layer inter-attention
class DeepAttention(nn.Module):
    def __init__(self, opt, abstr_list_cnt, deep_att_hidden_size_per_abstr, correlation_func=1, word_hidden_size=None):
        super(DeepAttention, self).__init__()

        word_hidden_size = opt['embedding_dim'] if word_hidden_size is None else word_hidden_size
        abstr_hidden_size = opt['hidden_size'] * 2

        att_size = abstr_hidden_size * abstr_list_cnt + word_hidden_size
        self.int_attn_list = nn.ModuleList()
        for i in range(abstr_list_cnt+1):
            self.int_attn_list.append(Attention(att_size, deep_att_hidden_size_per_abstr, correlation_func = correlation_func))

        rnn_input_size = abstr_hidden_size * abstr_list_cnt * 2 + (opt['highlvl_hidden_size'] * 2)

        self.rnn_input_size = rnn_input_size
        self.rnn, self.output_size = RNN_from_opt(rnn_input_size, opt['highlvl_hidden_size'], num_layers=1)

        self.opt = opt

    def forward(self, x1_word, x1_abstr, x2_word, x2_abstr, x1_mask, x2_mask, return_bef_rnn=False):
        """
        x1_word, x2_word, x1_abstr, x2_abstr are list of 3D tensors.
        3D tensor: batch_size * length * hidden_size
        """
        
        x1_att = torch.cat(x1_word + x1_abstr, 2)
        x2_att = torch.cat(x2_word + x2_abstr[:-1], 2)
        x1 = torch.cat(x1_abstr, 2)

        x2_list = x2_abstr
        for i in range(len(x2_list)):
            attn_hiddens = self.int_attn_list[i](x1_att, x2_att, x2_mask, x3=x2_list[i])
            x1 = torch.cat((x1, attn_hiddens), 2)

        x1_hiddens = self.rnn(x1, x1_mask)
        if return_bef_rnn:
            return x1_hiddens, x1
        else:
            return x1_hiddens

# bmm: batch matrix multiplication
# unsqueeze: add singleton dimension
# squeeze: remove singleton dimension
def weighted_avg(x, weights): # used in lego_reader.py
    """ 
        x = batch * len * d
        weights = batch * len
    """
    return weights.unsqueeze(1).bmm(x).squeeze(1)


###############################################################################
###############################################################################
#   https://github.com/SamLynnEvans/Transformer   #############################
###############################################################################
###############################################################################

###############################################################################
# From Embed.py
###############################################################################

class Embedder(nn.Module):
    def __init__(self, vocab_size, d_model):
        super().__init__()
        self.d_model = d_model
        self.embed = nn.Embedding(vocab_size, d_model)
    def forward(self, x):
        return self.embed(x)

class PositionalEncoder(nn.Module):
    def __init__(self, d_model, max_seq_len = 200, dropout = 0.1):
        super().__init__()
        self.d_model = d_model
        self.dropout = nn.Dropout(dropout)
        # create constant 'pe' matrix with values dependant on 
        # pos and i
        pe = torch.zeros(max_seq_len, d_model)
        for pos in range(max_seq_len):
            for i in range(0, d_model, 2):
                pe[pos, i] = \
                math.sin(pos / (10000 ** ((2 * i)/d_model)))
                pe[pos, i + 1] = \
                math.cos(pos / (10000 ** ((2 * (i + 1))/d_model)))
        pe = pe.unsqueeze(0)
        self.register_buffer('pe', pe)
 
    
    def forward(self, x):
        # make embeddings relatively larger
        x = x * math.sqrt(self.d_model)
        #add constant to embedding
        seq_len = x.size(1)
        pe = Variable(self.pe[:,:seq_len], requires_grad=False)
        if x.is_cuda:
            pe.cuda()
        x = x + pe
        return self.dropout(x)

###############################################################################
# From Sublayers.py
###############################################################################

class Norm(nn.Module):
    def __init__(self, d_model, eps = 1e-6):
        super().__init__()
    
        self.size = d_model
        
        # create two learnable parameters to calibrate normalisation
        self.alpha = nn.Parameter(torch.ones(self.size))
        self.bias = nn.Parameter(torch.zeros(self.size))
        
        self.eps = eps
    
    def forward(self, x):
        norm = self.alpha * (x - x.mean(dim=-1, keepdim=True)) \
        / (x.std(dim=-1, keepdim=True) + self.eps) + self.bias
        return norm

def attention(q, k, v, d_k, mask=None, dropout=None):
    
    scores = torch.matmul(q, k.transpose(-2, -1)) /  math.sqrt(d_k)
    
    if mask is not None:
        mask = mask.unsqueeze(1)
        scores = scores.masked_fill(mask == 0, -1e9)
    
    scores = F.softmax(scores, dim=-1)
    
    if dropout is not None:
        scores = dropout(scores)
        
    output = torch.matmul(scores, v)
    return output

class MultiHeadAttention(nn.Module):
    def __init__(self, heads, d_model, dropout = 0.1):
        super().__init__()
        
        self.d_model = d_model
        self.d_k = d_model // heads
        self.h = heads
        
        self.q_linear = nn.Linear(d_model, d_model)
        self.v_linear = nn.Linear(d_model, d_model)
        self.k_linear = nn.Linear(d_model, d_model)
        
        self.dropout = nn.Dropout(dropout)
        self.out = nn.Linear(d_model, d_model)
    
    def forward(self, q, k, v, mask=None):
        
        bs = q.size(0)
        
        # perform linear operation and split into N heads
        k = self.k_linear(k).view(bs, -1, self.h, self.d_k)
        q = self.q_linear(q).view(bs, -1, self.h, self.d_k)
        v = self.v_linear(v).view(bs, -1, self.h, self.d_k)
        
        # transpose to get dimensions bs * N * sl * d_model
        k = k.transpose(1,2)
        q = q.transpose(1,2)
        v = v.transpose(1,2)
        

        # calculate attention using function we will define next
        scores = attention(q, k, v, self.d_k, mask, self.dropout)
        # concatenate heads and put through final linear layer
        concat = scores.transpose(1,2).contiguous()\
        .view(bs, -1, self.d_model)
        output = self.out(concat)
    
        return output

class FeedForward(nn.Module):
    def __init__(self, d_model, d_ff=2048, dropout = 0.1):
        super().__init__() 
    
        # We set d_ff as a default to 2048
        self.linear_1 = nn.Linear(d_model, d_ff)
        self.dropout = nn.Dropout(dropout)
        self.linear_2 = nn.Linear(d_ff, d_model)
    
    def forward(self, x):
        x = self.dropout(F.relu(self.linear_1(x)))
        x = self.linear_2(x)
        return x

###############################################################################
# From Layers.py
###############################################################################

class EncoderLayer(nn.Module):
    def __init__(self, d_model, heads, dropout=0.1):
        super().__init__()
        self.norm_1 = Norm(d_model)
        self.norm_2 = Norm(d_model)
        self.attn = MultiHeadAttention(heads, d_model, dropout=dropout)
        self.ff = FeedForward(d_model, dropout=dropout)
        self.dropout_1 = nn.Dropout(dropout)
        self.dropout_2 = nn.Dropout(dropout)
        
    def forward(self, x, mask):
        x2 = self.norm_1(x)
        x = x + self.dropout_1(self.attn(x2,x2,x2,mask))
        x2 = self.norm_2(x)
        x = x + self.dropout_2(self.ff(x2))
        return x
    
# build a decoder layer with two multi-head attention layers and
# one feed-forward layer
class DecoderLayer(nn.Module):
    def __init__(self, d_model, heads, dropout=0.1):
        super().__init__()
        self.norm_1 = Norm(d_model)
        self.norm_2 = Norm(d_model)
        self.norm_3 = Norm(d_model)
        
        self.dropout_1 = nn.Dropout(dropout)
        self.dropout_2 = nn.Dropout(dropout)
        self.dropout_3 = nn.Dropout(dropout)
        
        self.attn_1 = MultiHeadAttention(heads, d_model, dropout=dropout)
        self.attn_2 = MultiHeadAttention(heads, d_model, dropout=dropout)
        self.ff = FeedForward(d_model, dropout=dropout)

    def forward(self, x, e_outputs, src_mask, trg_mask):
        x2 = self.norm_1(x)
        x = x + self.dropout_1(self.attn_1(x2, x2, x2, trg_mask))
        x2 = self.norm_2(x)
        x = x + self.dropout_2(self.attn_2(x2, e_outputs, e_outputs, \
        src_mask))
        x2 = self.norm_3(x)
        x = x + self.dropout_3(self.ff(x2))
        return x

###############################################################################
# From Models.py
###############################################################################

def get_clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for i in range(N)])

class Encoder(nn.Module):
    def __init__(self, vocab_size, d_model, N, heads, dropout):
        super().__init__()
        self.N = N
        self.embed = Embedder(vocab_size, d_model)
        self.pe = PositionalEncoder(d_model, dropout=dropout)
        self.layers = get_clones(EncoderLayer(d_model, heads, dropout), N)
        self.norm = Norm(d_model)
    def forward(self, src, mask):
        x = self.embed(src)
        x = self.pe(x)
        for i in range(self.N):
            x = self.layers[i](x, mask)
        return self.norm(x)
    
class Decoder(nn.Module):
    def __init__(self, vocab_size, d_model, N, heads, dropout):
        super().__init__()
        self.N = N
        self.embed = Embedder(vocab_size, d_model)
        self.pe = PositionalEncoder(d_model, dropout=dropout)
        self.layers = get_clones(DecoderLayer(d_model, heads, dropout), N)
        self.norm = Norm(d_model)
    def forward(self, trg, e_outputs, src_mask, trg_mask):
        x = self.embed(trg)
        x = self.pe(x)
        for i in range(self.N):
            x = self.layers[i](x, e_outputs, src_mask, trg_mask)
        return self.norm(x)

class Transformer(nn.Module):
    def __init__(self, src_vocab, trg_vocab, d_model, N, heads, dropout):
        super().__init__()
        self.encoder = Encoder(src_vocab, d_model, N, heads, dropout)
        self.decoder = Decoder(trg_vocab, d_model, N, heads, dropout)
        self.out = nn.Linear(d_model, trg_vocab)
        for p in self.parameters:
            nn.init.xavier_uniform_(p)
    def forward(self, src, trg, src_mask, trg_mask):
        e_outputs = self.encoder(src, src_mask)
        #print("DECODER")
        d_output = self.decoder(trg, e_outputs, src_mask, trg_mask)
        output = self.out(d_output)
        return output
