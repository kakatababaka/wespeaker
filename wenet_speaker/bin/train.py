# coding=utf-8
#!/usr/bin/env python3
import os
from pprint import pformat
import kaldiio
import fire, yaml
import tableprint as tp

import torch
import torch.distributed as dist
from torch.utils.data import Dataset, DataLoader
from torch.utils.data.distributed import DistributedSampler

from wenet_speaker.models import *
from wenet_speaker.utils.utils import *
from wenet_speaker.utils.file_utils import read_scp
from wenet_speaker.utils.schedulers import ExponentialDecrease, MarginScheduler
from wenet_speaker.utils.executor import runepoch
from wenet_speaker.utils.checkpoint import load_checkpoint, save_checkpoint
from wenet_speaker.dataset.dataset import FeatList_LableDict_Dataset


def train(config='conf/config.yaml', **kwargs):
    """Trains a model on the given features and spk labels.

    :config: A training configuration. Note that all parameters in the config can also be manually adjusted with --ARG VALUE
    :returns: None
    """

    configs = parse_config_or_kwargs(config, **kwargs)

    # dist configs
    rank = int(os.environ['LOCAL_RANK'])
    world_size = int(os.environ['WORLD_SIZE'])
    gpu = int(configs['gpus'][rank])
    os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu)
    dist.init_process_group(backend='nccl')

    model_dir = os.path.join(configs['exp_dir'], "models")
    if rank == 0:
        try:
            os.makedirs(model_dir)
        except IOError:
            print(model_dir+" already exists !!!")
            exit(1)
    dist.barrier()  # let the rank 0 mkdir first

    logger = genlogger(configs['exp_dir'], 'train.log')
    if world_size > 1:
        logger.info('training on multiple gpus, this gpu {}'.format(gpu))

    if rank == 0:
        logger.info("exp_dir is: {}".format(configs['exp_dir']))
        logger.info("<== Passed Arguments ==>")
        # Print arguments into logs
        for line in pformat(configs).split('\n'):
            logger.info(line)

    # seed
    set_seed(configs['seed'] + rank)

    # wav/feat
    train_scp = configs['dataset_args']['train_scp']
    train_label = configs['dataset_args']['train_label']
    train_utt_wav_list = read_scp(train_scp)
    if rank == 0:
        logger.info("<== Feature ==>")
        logger.info("train wav/feat num: {}".format(len(train_utt_wav_list)))

    # spk label
    train_utt_spk_list = read_scp(train_label)
    spk2id_dict = spk2id(train_utt_spk_list)
    train_utt2spkid_dict = {utt_spk[0]:spk2id_dict[utt_spk[1]] for utt_spk in train_utt_spk_list}
    if rank == 0:
        logger.info("<== Labels ==>")
        logger.info("train label num: {}, spk num: {}".format(len(train_utt2spkid_dict),len(spk2id_dict)))

    # dataset and dataloader
    configs['feature_args']['feat_dim'] = configs['model_args']['feat_dim']
    train_dataset = FeatList_LableDict_Dataset(train_utt_wav_list, train_utt2spkid_dict, **configs['feature_args'], **configs['dataset_args'])
    train_sampler = DistributedSampler(train_dataset, shuffle=True)
    train_dataloader = DataLoader(train_dataset, sampler=train_sampler, **configs['dataloader_args'])
    if rank == 0:
        logger.info("<== Dataloaders ==>")
        logger.info("train dataloaders created")

    # model
    logger.info("<== Model ==>")
    model = eval(configs['model'])(**configs['model_args'])
    if configs['model_init'] is not None:
        logger.info('Load intial model from {}'.format(configs['model_init']))
        load_checkpoint(model, configs['model_init'])
    else:
        logger.info('Train model from scratch...')
        # projection
        configs['projection_args']['embed_dim'] = configs['model_args']['embed_dim']
        configs['projection_args']['num_class'] = len(spk2id_dict)
        if configs['dataset_args']['speed_perturb']:
            configs['projection_args']['num_class'] *= 3 # diff speed is regarded as diff spk
        projection = get_projection(configs['projection_args'])
        model.add_module("projection", projection)
    if rank == 0:
        for line in pformat(model).split('\n'):
            logger.info(line)
    
    # ddp_model 
    model.cuda()
    ddp_model = torch.nn.parallel.DistributedDataParallel(model) #, find_unused_parameters=True)
    device = torch.device("cuda")

    criterion = getattr(torch.nn, configs['loss'])(**configs['loss_args'])
    if rank == 0:
        logger.info("<== Loss ==>")
        logger.info("loss criterion is: "+configs['loss'])

    configs['optimizer_args']['lr'] = configs['scheduler_args']['initial_lr']
    optimizer = getattr(torch.optim, configs['optimizer'])(ddp_model.parameters(), **configs['optimizer_args'])
    if rank == 0:
        logger.info("<== Optimizer ==>")
        logger.info("optimizer is: "+configs['optimizer'])

    train_configs = configs['train_configs']
    train_configs['num_epochs'] = int(train_configs['num_epochs'] / (1.0 - configs['dataset_args']['aug_prob'])) # add num_epochs
    configs['scheduler_args']['num_epochs'] =  train_configs['num_epochs']
    configs['scheduler_args']['epoch_iter'] = len(train_dataloader)
    configs['scheduler_args']['process_num'] = world_size
    scheduler = eval(configs['scheduler'])(optimizer, **configs['scheduler_args'])
    if rank == 0:
        logger.info("<== Scheduler ==>")
        logger.info("scheduler is: "+configs['scheduler'])

    configs['margin_update']['epoch_iter'] = len(train_dataloader)
    margin_scheduler = MarginScheduler(model=model, **configs['margin_update'])
    if rank == 0:
        logger.info("<== MarginScheduler ==>")

    # save config.yam
    if rank == 0:
        saved_config_path = os.path.join(configs['exp_dir'], 'config.yaml')
        with open(saved_config_path, 'w') as fout:
            data = yaml.dump(configs)
            fout.write(data) 

    # training 
    dist.barrier() # synchronize here
    if rank == 0:
        logger.info("<========== Training process ==========>")    
        header = ['Epoch', 'Batch', 'Lr', 'Margin', 'Loss', "Acc"]
        for line in tp.header(header, width=10, style='grid').split('\n'):
            logger.info(line)
    dist.barrier() # synchronize here

    for epoch in range(1, train_configs['num_epochs']+1):
        train_sampler.set_epoch(epoch)

        runepoch(train_dataloader, ddp_model, criterion, optimizer, scheduler, margin_scheduler, epoch, logger, log_batch_interval=train_configs['log_batch_interval'], device=device)

        if rank == 0:
            if epoch % train_configs['save_epoch_interval'] == 0 or epoch >= train_configs['num_epochs'] - configs['num_avg']:
                save_checkpoint(model, os.path.join(model_dir, 'model_{}.pt'.format(epoch)))
                
    if rank == 0:
        os.symlink('model_{}.pt'.format(train_configs['num_epochs']), os.path.join(model_dir,'final_model.pt'))
        logger.info(tp.bottom(len(header), width=10, style='grid'))


if __name__ == '__main__':
    fire.Fire(train)
