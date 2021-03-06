import argparse
import time
import numpy as np
import os

import tensorflow as tf
from model_new import *
from train_util import *
from io_util import get_class_names,get_block_train_test_split,read_pkl
from provider import Provider,default_unpack_feats_labels
from draw_util import output_points,get_class_colors

parser = argparse.ArgumentParser()
parser.add_argument('--num_gpus', type=int, default=4, help='')
parser.add_argument('--batch_size', type=int, default=1, help='')

parser.add_argument('--lr_init', type=float, default=1e-3, help='')
parser.add_argument('--lr_clip', type=float, default=1e-5, help='')
parser.add_argument('--decay_rate', type=float, default=0.5, help='')
parser.add_argument('--decay_epoch', type=int, default=50, help='')
parser.add_argument('--num_classes', type=int, default=13, help='')

parser.add_argument('--restore',type=bool, default=False, help='')
parser.add_argument('--restore_epoch', type=int, default=0, help='')
parser.add_argument('--restore_model', type=str, default='', help='')

parser.add_argument('--log_step', type=int, default=1000, help='')
parser.add_argument('--train_dir', type=str, default='train/pgnet_v2', help='')
parser.add_argument('--save_dir', type=str, default='model/pgnet_v2', help='')
parser.add_argument('--log_file', type=str, default='pgnet_v2.log', help='')
parser.add_argument('--use_root', type=bool, default=False, help='')
parser.add_argument('--use_diffusion', type=bool, default=False, help='')
parser.add_argument('--test_subset', type=bool, default=False, help='')


parser.add_argument('--eval',type=bool, default=False, help='')
parser.add_argument('--eval_model',type=str, default='model/label/unsupervise80.ckpt',help='')

parser.add_argument('--train_epoch_num', type=int, default=100, help='')

FLAGS = parser.parse_args()


train_weights=[2.80089331,2.92693353,2.94871211,5.12748384,5.07317114,
               5.18505001,4.612535,4.83436918,4.1070838,5.36530066,4.64813137,
               5.26789713,3.67803526]
train_weights=np.asarray(train_weights)


def tower_loss(xyzs, feats, labels, is_training, reuse=False):
    with tf.variable_scope(tf.get_variable_scope(),reuse=reuse):
        xyzs, dxyzs, feats, labels, vlens, vbegs, vcens = \
            points_pooling_two_layers(xyzs,feats,labels,voxel_size1=0.2,voxel_size2=0.8,block_size=3.0)
        global_feats,local_feats,_=edge_condition_diffusion_anchor_model_v2(
            xyzs, dxyzs, feats, vlens, vbegs, vcens, [0.2, 0.8], 3.0, [0.1,0.4,1.6], reuse)

        global_feats=tf.expand_dims(global_feats,axis=0)
        local_feats=tf.expand_dims(local_feats,axis=0)
        logits=classifier_v3(global_feats, local_feats, is_training, FLAGS.num_classes, reuse, use_bn=False)  # [1,pn,num_classes]
        # logits,xyzs,labels=pgnet_nse_whole(xyzs,feats,labels,reuse,is_training,FLAGS.num_classes)

        flatten_logits = tf.reshape(logits, [-1, FLAGS.num_classes])  # [pn,num_classes]

        # loss
        labels_flatten=tf.reshape(labels,[-1,1])                  # [pn,1]
        labels_flatten=tf.squeeze(labels_flatten,axis=1)          # [pn]
        train_weights_tf=tf.Variable(train_weights,trainable=False,name='train_weights')
        weights=tf.gather(train_weights_tf,labels_flatten)
        loss=tf.losses.sparse_softmax_cross_entropy(labels_flatten,flatten_logits,weights=weights)
        # loss=tf.losses.sparse_softmax_cross_entropy(labels_flatten,flatten_logits)

    tf.summary.scalar(loss.op.name,loss)

    return loss,flatten_logits,labels


def train_ops(xyzs, feats, labels, is_training, epoch_batch_num):
    ops={}
    with tf.device('/cpu:0'):
        global_step = tf.get_variable(
            'global_step', [],
            initializer=tf.constant_initializer(0), trainable=False)

        decay_steps=epoch_batch_num*FLAGS.decay_epoch
        lr=tf.train.exponential_decay(FLAGS.lr_init,global_step,decay_steps,FLAGS.decay_rate,staircase=True)
        lr=tf.maximum(FLAGS.lr_clip,lr)
        tf.summary.scalar('learning rate',lr)

        opt=tf.train.AdamOptimizer(lr)

        reuse=False
        tower_grads=[]
        tower_losses=[]
        tower_logits=[]
        tower_labels=[]
        for i in range(FLAGS.num_gpus):
            with tf.device('/gpu:{}'.format(i%4)):
                with tf.name_scope('tower_{}'.format(i)):
                    loss,logits,label=tower_loss(xyzs[i], feats[i], labels[i], is_training, reuse)

                    grad=opt.compute_gradients(loss)
                    tower_grads.append(grad)
                    tower_losses.append(loss)
                    tower_logits.append(logits)
                    tower_labels.append(label)
                    update_op = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
                    summaries = tf.get_collection(tf.GraphKeys.SUMMARIES)
                    reuse=True

        avg_grad=average_gradients(tower_grads)

        with tf.control_dependencies(update_op):
            apply_grad_op=tf.group(opt.apply_gradients(avg_grad,global_step=global_step))
        summary_op = tf.summary.merge(summaries)

        total_loss_op=tf.add_n(tower_losses)/FLAGS.num_gpus
        tower_labels=tf.concat(tower_labels,axis=0)

        logits_op=tf.concat(tower_logits,axis=0)
        preds_op=tf.argmax(logits_op,axis=1)
        correct_num_op=tf.reduce_sum(tf.cast(tf.equal(preds_op,tower_labels),tf.float32))

        ops['total_loss']=total_loss_op
        ops['apply_grad']=apply_grad_op
        ops['logits']=logits_op
        ops['preds']=preds_op
        ops['correct_num']=correct_num_op
        ops['summary']=summary_op
        ops['global_step']=global_step
        ops['labels']=tower_labels
        ops['lr']=lr

    return ops


def train_one_epoch(ops,pls,sess,summary_writer,trainset,epoch_num,feed_dict):
    epoch_begin=time.time()
    total_correct,total_block,total_points=0,0,0
    begin_time=time.time()
    total_losses=[]

    for i,feed_in in enumerate(trainset):
        batch_pt_num,_,_=fill_feed_dict(feed_in,feed_dict,pls,FLAGS.num_gpus)

        feed_dict[pls['is_training']]=True
        total_block+=FLAGS.num_gpus
        total_points+=batch_pt_num

        _,loss_val,correct_num=sess.run([ops['apply_grad'],ops['total_loss'],ops['correct_num']],feed_dict)

        total_losses.append(loss_val)
        total_correct+=correct_num
        if i*FLAGS.num_gpus % FLAGS.log_step==0:
            summary,global_step,lr=sess.run(
                [ops['summary'],ops['global_step'],ops['lr']],feed_dict)

            log_str('epoch {} step {} loss {:.5} acc {:.5} | {:.5} examples/s lr {:.5}'.format(
                epoch_num,i,np.mean(np.asarray(total_losses)),
                float(total_correct)/total_points,
                float(total_block)/(time.time()-begin_time),lr
            ),FLAGS.log_file)

            summary_writer.add_summary(summary,global_step)
            total_correct,total_block,total_points=0,0,0
            begin_time=time.time()
            total_losses=[]

        # test every 11600(half the training set)
        if i*FLAGS.num_gpus>7500:
            break

    log_str('epoch {} cost {} s'.format(epoch_num, time.time()-epoch_begin), FLAGS.log_file)


def test_one_epoch(ops,pls,sess,saver,testset,epoch_num,feed_dict,summary_writer=None):
    begin_time=time.time()
    test_loss=[]
    all_preds,all_labels=[],[]
    for i,feed_in in enumerate(testset):
        fill_feed_dict(feed_in,feed_dict,pls,FLAGS.num_gpus)
        feed_dict[pls['is_training']] = False

        loss,batch_labels,preds=sess.run([ops['total_loss'],ops['labels'],ops['preds']],feed_dict)
        test_loss.append(loss)
        all_preds.append(preds)
        all_labels.append(batch_labels)

    all_preds=np.concatenate(all_preds,axis=0)
    all_labels=np.concatenate(all_labels,axis=0)

    test_loss=np.mean(np.asarray(test_loss))

    iou, miou, oiou, acc, macc, oacc = compute_iou(all_labels,all_preds)

    log_str('mean iou {:.5} overall iou {:5} loss {:5} \n mean acc {:5} overall acc {:5} cost {:3} s'.format(
        miou, oiou, test_loss, macc, oacc, time.time()-begin_time
    ),FLAGS.log_file)

    if not FLAGS.eval:
        checkpoint_path = os.path.join(FLAGS.save_dir, 'model{}.ckpt'.format(epoch_num))
        saver.save(sess,checkpoint_path)
    else:
        names=get_class_names()
        for i in xrange(len(names)):
            print '{} iou {} acc {}'.format(names[i],iou[i],acc[i])


def fill_feed_dict(feed_in,feed_dict,pls,num_gpus):
    cxyzs, rgbs, covars, lbls, block_mins = default_unpack_feats_labels(feed_in, num_gpus)
    batch_pt_num=0
    batch_labels=[]
    for k in xrange(num_gpus):
        feed_dict[pls['xyzs'][k]]=cxyzs[k]
        # feats=np.concatenate([rgbs[k],covars[k]],axis=1)
        # feed_dict[pls['feats'][k]]=feats
        feed_dict[pls['feats'][k]]=rgbs[k]
        feed_dict[pls['lbls'][k]]=lbls[k]

        batch_pt_num += lbls[k].shape[0]
        batch_labels.append(lbls[k])

    return batch_pt_num,batch_labels,block_mins


def build_placeholder(num_gpus):
    pls = {}
    pls['xyzs'], pls['feats'], pls['lbls']=[],[],[]
    for i in xrange(num_gpus):
        pls['xyzs'].append(tf.placeholder(tf.float32,[None,3],'xyzs{}'.format(i)))
        pls['feats'].append(tf.placeholder(tf.float32,[None,3],'feats{}'.format(i)))
        # pls['feats'].append(tf.placeholder(tf.float32,[None,12],'feats{}'.format(i)))
        pls['lbls'].append(tf.placeholder(tf.int32,[None],'lbls{}'.format(i)))

    pls['is_training'] = tf.placeholder(tf.bool, name='is_training')
    return pls


def train():
    import random
    train_list,test_list=get_block_train_test_split()
    train_list=['data/S3DIS/sampled_train_new/'+fn for fn in train_list]
    # with open('cached/s3dis_merged_train.txt', 'r') as f:
    #     train_list=[line.strip('\n') for line in f.readlines()]
    random.shuffle(train_list)
    test_list=['data/S3DIS/sampled_test_new/'+fn for fn in test_list]

    def fn(model,filename):
        data=read_pkl(filename)
        return data

    train_provider = Provider(train_list,'train',FLAGS.batch_size*FLAGS.num_gpus,fn)
    test_provider = Provider(test_list,'test',FLAGS.batch_size*FLAGS.num_gpus,fn)

    try:
        pls=build_placeholder(FLAGS.num_gpus)
        batch_num_per_epoch=2000/FLAGS.num_gpus
        ops=train_ops(pls['xyzs'],pls['feats'],pls['lbls'],pls['is_training'],batch_num_per_epoch)

        feed_dict={}
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        config.allow_soft_placement = True
        config.log_device_placement = False
        sess = tf.Session(config=config)
        saver = tf.train.Saver(max_to_keep=500)
        if FLAGS.restore:
              saver.restore(sess,FLAGS.restore_model)
        else:
            sess.run(tf.global_variables_initializer())

        summary_writer = tf.summary.FileWriter(FLAGS.train_dir,graph=sess.graph)

        for epoch_num in xrange(FLAGS.restore_epoch,FLAGS.train_epoch_num):
            train_one_epoch(ops,pls,sess,summary_writer,train_provider,epoch_num,feed_dict)
            test_one_epoch(ops,pls,sess,saver,test_provider,epoch_num,feed_dict)

    finally:
        train_provider.close()
        test_provider.close()


def eval():
    train_list,test_list=get_block_train_test_split()
    test_list=['data/S3DIS/sampled_test/'+fn for fn in test_list]


    def fn(model,filename):
        data=read_pkl(filename)
        xyzs=[t[0] for t in data[0]]
        return xyzs,data[2],data[3],data[4],data[12]

    # def fn(model,filename):
    #     data=read_pkl(filename)
    #     return data

    test_provider = Provider(test_list,'test',FLAGS.batch_size*FLAGS.num_gpus,fn)

    try:
        pls=build_placeholder(FLAGS.num_gpus)
        batch_num_per_epoch=2000/FLAGS.num_gpus
        ops=train_ops(pls['xyzs'],pls['feats'],pls['lbls'],pls['is_training'],batch_num_per_epoch)

        feed_dict={}
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        config.allow_soft_placement = True
        config.log_device_placement = False
        sess = tf.Session(config=config)

        saver = tf.train.Saver(max_to_keep=500)
        saver.restore(sess,FLAGS.eval_model)
        summary_writer = tf.summary.FileWriter(FLAGS.train_dir,graph=sess.graph)
        test_one_epoch(ops,pls,sess,saver,test_provider,0,feed_dict,summary_writer)

    finally:
        test_provider.close()


if __name__=="__main__":
    if FLAGS.eval: eval()
    else: train()