# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from datetime import datetime
import json
import numpy as np
import os
import random
import sys
import time
import torch
import csv
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from Utils.CoQAPreprocess import CoQAPreprocess
from Models.Layers import MaxPooling, set_dropout_prob
from Models.SDNet import SDNet
from Models.BaseTrainer import BaseTrainer
from Utils.CoQAUtils import BatchGen, AverageMeter, gen_upper_triangle, score
 
class SDNetTrainer(BaseTrainer):
    def __init__(self, opt):
        super(SDNetTrainer, self).__init__(opt)
        print('SDNet Model Trainer')
        set_dropout_prob(0.0 if not 'DROPOUT' in opt else float(opt['DROPOUT']))
        self.seed = int(opt['SEED'])
        self.data_prefix = 'coqa-'
        self.save_dir = str(opt['SAVE_DIR']) + str(opt['SPLIT']) + '_' + str(opt['SUB_FILE'])
        random.seed(self.seed)
        np.random.seed(self.seed)
        torch.manual_seed(self.seed)
        print(type(opt))
        self.opt2 = opt.copy()
        self.opt2['FEATURE_FOLDER'] = 'conf~/coqa_spacy_intermediate_feature~/'
        self.opt2['FEATURE_FOLDER'] = os.path.join(opt['datadir'], self.opt2['FEATURE_FOLDER'])
        self.preproc = CoQAPreprocess(self.opt)
        self.preproc_2 = CoQAPreprocess(self.opt2)
        if self.use_cuda:
            torch.cuda.manual_seed_all(self.seed)

    def official(self, model_path, test_data):
        print('-----------------------------------------------')
        print("Initializing model...")
        self.setup_model(self.preproc.train_embedding)
        path = os.path.join(self.opt['datadir'], model_path)
        self.load_model(path)

        print("Predicting in batches...")
        test_batches = BatchGen(self.opt, test_data['data'], self.use_cuda, self.preproc.train_vocab, self.preproc.train_char_vocab, evaluation=True)
        predictions = []
        confidence = []
        final_json = []
        submissions = {}
        cnt = 0
        for j, test_batch in enumerate(test_batches):
            cnt += 1
            if cnt % 50 == 0:
                print(cnt, '/', len(test_batches))  
            phrase, phrase_score, pred_json, submission = self.predict(test_batch)
            predictions.extend(phrase)
            confidence.extend(phrase_score)
            final_json.extend(pred_json)
            submissions.update(submission)

        return predictions, confidence, final_json, submissions

    def train(self): 
        self.isTrain = True
        self.getSaveFolder()
        self.saveConf()
        self.vocab, self.char_vocab, vocab_embedding = self.preproc.load_data()
        self.vocab2, self.char_vocab2, vocab_embedding2 = self.preproc_2.load_data()
        self.log('-----------------------------------------------')
        self.log("Initializing model...")
        self.setup_model(vocab_embedding)
        
        if 'RESUME' in self.opt:
            model_path = os.path.join(self.opt['datadir'], self.opt['MODEL_PATH'])
            self.load_model(model_path)            

        print('Loading train json...')
        with open(os.path.join(self.opt['FEATURE_FOLDER'], self.data_prefix + 'train-preprocessed.json'), 'r') as f:
            train_data = json.load(f)
            # train_data['data'] = train_data['data'][:20]

        print('Loading dev json...')
        with open(os.path.join(self.opt['FEATURE_FOLDER'], self.data_prefix + 'dev-preprocessed.json'), 'r') as f:
            dev_data = json.load(f)

        with open(os.path.join(self.opt2['FEATURE_FOLDER'], self.data_prefix + 'train-preprocessed.json'), 'r') as f:
            train_data2 = json.load(f)
            # train_data['data'] = train_data['data'][:20]

        print('Loading dev json...')
        with open(os.path.join(self.opt2['FEATURE_FOLDER'], self.data_prefix + 'dev-preprocessed.json'), 'r') as f:
            dev_data2 = json.load(f)
            # dev_data['data'] = dev_data['data'][:20]
        best_f1_score = 0.0
        numEpochs = self.opt['EPOCH']
        squad = True
        train_batches2 = BatchGen(self.opt2, train_data2['data'], self.use_cuda, self.vocab2, self.char_vocab2, squad=False)
        dev_batches2 = BatchGen(self.opt2, dev_data2['data'], self.use_cuda, self.vocab2, self.char_vocab2, evaluation=True, squad=False)
        for epoch in range(self.epoch_start, numEpochs):
            self.log('Epoch {}'.format(epoch))
            self.network.train()
            startTime = datetime.now()
            train_batches = BatchGen(self.opt, train_data['data'], self.use_cuda, self.vocab, self.char_vocab)
            dev_batches = BatchGen(self.opt, dev_data['data'], self.use_cuda, self.vocab, self.char_vocab, evaluation=True)
            data_zipped = zip(train_batches, train_batches2)
            train_list = list(train_batches)
            coqa_list = list(train_batches2)
            iterate_len = min(len(train_list), len(coqa_list))
            for i in range(iterate_len):
            #for i, data_zip in enumerate(data_zipped):
                if i == len(train_batches) - 1 or (epoch == 0 and i == 0 and ('RESUME' in self.opt)) or (i % 1500 == 0):
                    print('Saving folder is', self.saveFolder)
                    print('Evaluating on dev set...')
                    predictions = []
                    confidence = []
                    dev_answer = []
                    final_json = []
                    submission = {}
                    for j, dev_batch in enumerate(dev_batches):
                        phrase, phrase_score, pred_json, submission_batch = self.predict(dev_batch)
                        final_json.extend(pred_json)
                        predictions.extend(phrase)
                        confidence.extend(phrase_score)
                        dev_answer.extend(dev_batch[-4]) # answer_str
                        submission.update(submission_batch)
                    result, all_f1s = score(predictions, dev_answer, final_json)
                    f1 = result['f1']
                    if f1 > best_f1_score:
                        model_file = os.path.join(self.saveFolder, 'best_model.pt')
                        self.save_for_predict(model_file, epoch)
                        best_f1_score = f1
                        pred_json_file = os.path.join(self.saveFolder, 'prediction.json')
                        with open(pred_json_file, 'w') as output_file:
                            json.dump(final_json, output_file)
                        score_per_instance = []    
                        for instance, s in zip(final_json, all_f1s):
                            score_per_instance.append({
                                'id': instance['id'],
                                'turn_id': instance['turn_id'],
                                'f1': s
                            })
                        score_per_instance_json_file = os.path.join(self.saveFolder, 'score_per_instance.json')
                        with open(score_per_instance_json_file, 'w') as output_file:
                            json.dump(score_per_instance, output_file)    

                        sub_path = self.save_dir
                        with open(sub_path, 'w') as csv_fh:
                            csv_writer = csv.writer(csv_fh, delimiter=',')
                            csv_writer.writerow(['Id', 'Predicted'])
                            for uuid in sorted(submission):
                                csv_writer.writerow([uuid,submission[uuid]])

                    self.log("Epoch {0} - dev: F1: {1:.3f} (best F1: {2:.3f})".format(epoch, f1, best_f1_score))
                    self.log("Results breakdown\n{0}".format(result))

                self.update(train_list[i], True)
                self.update(coqa_list[i % 50], False)
                if i % 100 == 0:
                    self.log('updates[{0:6}] train loss[{1:.5f}] remaining[{2}]'.format(
                        self.updates, self.train_loss.avg,
                        str((datetime.now() - startTime) / (i + 1) * (len(train_batches) - i - 1)).split('.')[0]))
                    #torch.cuda.empty_cache()


            print("PROGRESS: {0:.2f}%".format(100.0 * (epoch + 1) / numEpochs))
            print('Config file is at ' + self.opt['confFile'])

    def setup_model(self, vocab_embedding):
        self.train_loss = AverageMeter()
        self.network = SDNet(self.opt, vocab_embedding)
        if self.use_cuda:
            self.log('Putting model into GPU')
            self.network.cuda()

        parameters = [p for p in self.network.parameters() if p.requires_grad]
        self.optimizer = optim.Adamax(parameters)
        if 'ADAM2' in self.opt:
            print('ADAM2')
            self.optimizer = optim.Adam(parameters, lr = 0.0001)

        self.updates = 0
        self.epoch_start = 0
        self.loss_func = F.cross_entropy 

    def update(self, batch, squad):
        # Train mode
        self.network.train()
        self.network.drop_emb = True

        x, x_mask, x_char, x_char_mask, x_features, x_pos, x_ent, x_bert, x_bert_mask, x_bert_offsets, query, query_mask, \
        query_char, query_char_mask, query_bert, query_bert_mask, query_bert_offsets, ground_truth, context_str, context_words, _, _, _, _, temp = batch

        # Run forward
        # score_s, score_e: batch x context_word_num
        # score_yes, score_no, score_no_answer: batch x 1
        # score_s, score_e, score_no_answer = self.network(x, x_mask, x_char, x_char_mask, x_features, x_pos, x_ent, x_bert, x_bert_mask, x_bert_offsets, 
        #     query, query_mask, query_char, query_char_mask, query_bert, query_bert_mask, query_bert_offsets, len(context_words))
        score_s, score_e, score_yes, score_no, score_no_answer = self.network(x, x_mask, x_char, x_char_mask, x_features, x_pos, x_ent, x_bert, x_bert_mask, x_bert_offsets, 
            query, query_mask, query_char, query_char_mask, query_bert, query_bert_mask, query_bert_offsets, len(context_words))
        max_len = self.opt['max_len'] or score_s.size(1)
        batch_size = score_s.shape[0]
        context_len = score_s.size(1)
        expand_score = gen_upper_triangle(score_s, score_e, max_len, self.use_cuda)
        if False:
            score_yes = torch.zeros(score_yes.shape).cuda()
            score_no = torch.zeros(score_no.shape).cuda()
            scores = torch.cat((expand_score, score_no, score_yes, score_no_answer), dim=1) # batch x (context_len * context_len + 3)
            targets = []
            span_idx = int(context_len * context_len)
            for i in range(ground_truth.shape[0]):
                ground_truth_0 = np.asarray(ground_truth[i][0].cpu())
                ground_truth_1 = np.asarray(ground_truth[i][1].cpu())
                if ground_truth_0 == -1 and ground_truth_1 == -1: # no answer
                    targets.append(span_idx + 2)
                else:
                    targets.append(ground_truth_0 * context_len + ground_truth_1)
            targets = torch.LongTensor(np.array(targets))
            if self.use_cuda:
                targets = targets.cuda()
            loss = self.loss_func(scores, targets)
            self.train_loss.update(loss.item(), 1)
            self.optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm(self.network.parameters(), self.opt['grad_clipping'])
            self.optimizer.step()
            self.updates += 1
        if True:
            score_s, score_e, score_yes, score_no, score_no_answer = self.network(x, x_mask, x_char, x_char_mask, x_features, x_pos, x_ent, x_bert, x_bert_mask, x_bert_offsets, 
            query, query_mask, query_char, query_char_mask, query_bert, query_bert_mask, query_bert_offsets, len(context_words))
            max_len = self.opt['max_len'] or score_s.size(1)
            batch_size = score_s.shape[0]
            context_len = score_s.size(1)
            expand_score = gen_upper_triangle(score_s, score_e, max_len, self.use_cuda)
            scores = torch.cat((expand_score, score_no, score_yes, score_no_answer), dim=1) # batch x (context_len * context_len + 3)
            targets = []
            span_idx = int(context_len * context_len)
            for i in range(ground_truth.shape[0]):
                ground_truth_0 = np.asarray(ground_truth[i][0].cpu())
                ground_truth_1 = np.asarray(ground_truth[i][1].cpu())
                if ground_truth_0 == -1 and ground_truth_1 == -1: # no answer
                    targets.append(span_idx + 2)
                if ground_truth_0 == 0 and ground_truth_1 == -1: # no
                    targets.append(span_idx)
                if ground_truth_0 == -1 and ground_truth_1 == 0: # yes
                    targets.append(span_idx + 1)
                if ground_truth_0 != -1 and ground_truth_1 != -1: # normal span
                    targets.append(ground_truth_0 * context_len + ground_truth_1)
            targets = torch.LongTensor(np.array(targets))

            if self.use_cuda:
                targets = targets.cuda()
            loss = self.loss_func(scores, targets)
            if False:
                self.train_loss.update(loss.data[0], 1)
            else:
                self.train_loss.update(loss.data.item(), 1)
            self.optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm(self.network.parameters(), self.opt['grad_clipping'])
            self.optimizer.step()
            self.updates += 1
        if 'TUNE_PARTIAL' in self.opt:
            self.network.vocab_embed.weight.data[self.opt['tune_partial']:] = self.network.fixed_embedding

    def predict(self, batch, squad=True):
        self.network.eval()
        self.network.drop_emb = False

        # Run forward
        x, x_mask, x_char, x_char_mask, x_features, x_pos, x_ent, x_bert, x_bert_mask, x_bert_offsets, query, query_mask, \
        query_char, query_char_mask, query_bert, query_bert_mask, query_bert_offsets, ground_truth, context_str, context_words, \
        context_word_offsets, answers, context_id, turn_ids, data_ids = batch
        
        context_len = len(context_words)
        score_s, score_e, score_yes, score_no, score_no_answer = self.network(x, x_mask, x_char, x_char_mask, x_features, x_pos, x_ent, x_bert, x_bert_mask, x_bert_offsets, 
            query, query_mask, query_char, query_char_mask, query_bert, query_bert_mask, query_bert_offsets, len(context_words))
        # score_s, score_e, score_no_answer = self.network(x, x_mask, x_char, x_char_mask, x_features, x_pos, x_ent, x_bert, x_bert_mask, x_bert_offsets, 
        #     query, query_mask, query_char, query_char_mask, query_bert, query_bert_mask, query_bert_offsets, len(context_words))
        batch_size = score_s.shape[0]
        max_len = self.opt['max_len'] or score_s.size(1)

        expand_score = gen_upper_triangle(score_s, score_e, max_len, self.use_cuda)
        # scores = torch.cat((expand_score, score_no, score_yes, score_no_answer), dim=1) # batch x (context_len * context_len + 3)       
        scores = torch.cat((expand_score, score_no_answer), dim=1) # batch x (context_len * context_len + 3)

        prob = F.softmax(scores, dim = 1).data.cpu() # Transfer to CPU/normal tensors for numpy ops

        # Get argmax text spans
        predictions = []
        confidence = []
        
        pred_json = []
        submission_res = {}
        if squad:
            for i in range(batch_size):
                _, ids = torch.sort(prob[i, :], descending=True)
                idx = 0
                best_id = ids[idx]

                confidence.append(float(prob[i, best_id]))
                if best_id < context_len * context_len:
                    st = best_id / context_len
                    ed = best_id % context_len
                    st = context_word_offsets[st][0]
                    ed = context_word_offsets[ed][1]
                    output_str = context_str[st:ed]
                    invalid = ['.', ':', ']', ')', '?', '>']
                    if output_str in invalid:
                        predictions.append('unknown')
                    else:
                        predictions.append(context_str[st:ed])
                
                if best_id == context_len * context_len:
                    predictions.append('unknown')

                if best_id == context_len * context_len + 1:
                    predictions.append('unknown')

                if best_id == context_len * context_len + 2:
                    predictions.append('unknown')

                pred_json.append({
                    'id': context_id,
                    'turn_id': turn_ids[i],
                    'answer': predictions[-1]
                })
                text = '' if predictions[-1] == 'unknown' else predictions[-1]
                submission_res.update({data_ids[i]: text})
            return (predictions, confidence, pred_json, submission_res) # list of strings, list of floats, list of jsons
        else:
            for i in range(batch_size):
                _, ids = torch.sort(prob[i, :], descending=True)
                idx = 0
                best_id = ids[idx]

                confidence.append(float(prob[i, best_id]))
                if best_id < context_len * context_len:
                    st = best_id / context_len
                    ed = best_id % context_len
                    st = context_word_offsets[st][0]
                    ed = context_word_offsets[ed][1]
                    predictions.append(context_str[st:ed])
                
                if best_id == context_len * context_len:
                    predictions.append('no')

                if best_id == context_len * context_len + 1:
                    predictions.append('yes')

                if best_id == context_len * context_len + 2:
                    predictions.append('unknown')

                pred_json.append({
                    'id': context_id,
                    'turn_id': turn_ids[i],
                    'answer': predictions[-1]
                })

            return (predictions, confidence, pred_json) # list of strings, list of floats, list of jsons

    def load_model(self, model_path):
        print('Loading model from', model_path)
        checkpoint = torch.load(model_path)
        state_dict = checkpoint['state_dict']
        new_state = set(self.network.state_dict().keys())
        for k in list(state_dict['network'].keys()):
            if k not in new_state:
                del state_dict['network'][k]
        for k, v in list(self.network.state_dict().items()):
            if k not in state_dict['network']:
                state_dict['network'][k] = v
        self.network.load_state_dict(state_dict['network'])

        print('Loading finished', model_path)        

    def save(self, filename, epoch, prev_filename):
        params = {
            'state_dict': {
                'network': self.network.state_dict(),
                'optimizer': self.optimizer.state_dict(),
                'updates': self.updates # how many updates
            },
            'train_loss': {
                'val': self.train_loss.val,
                'avg': self.train_loss.avg,
                'sum': self.train_loss.sum,
                'count': self.train_loss.count
            },
            'config': self.opt,
            'epoch': epoch
        }
        try:
            torch.save(params, filename)
            self.log('model saved to {}'.format(filename))
            if os.path.exists(prev_filename):
                os.remove(prev_filename)
        except BaseException:
            self.log('[ WARN: Saving failed... continuing anyway. ]')

    def save_for_predict(self, filename, epoch):
        network_state = dict([(k, v) for k, v in self.network.state_dict().items() if k[0:4] != 'CoVe' and k[0:4] != 'ELMo' and k[0:9] != 'AllenELMo' and k[0:4] != 'Bert'])

        if 'eval_embed.weight' in network_state:
            del network_state['eval_embed.weight']
        if 'fixed_embedding' in network_state:
            del network_state['fixed_embedding']
        params = {
            'state_dict': {'network': network_state},
            'config': self.opt,
        }
        try:
            torch.save(params, filename)
            self.log('model saved to {}'.format(filename))
        except BaseException:
            self.log('[ WARN: Saving failed... continuing anyway. ]')
