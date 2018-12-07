import argparse
import subprocess
import tensorflow as tf
import threading
import numpy as np
# import scipy.io
import provider
from datetime import datetime
import json
import os
import sys
import glob
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)
sys.path.append(os.path.dirname(BASE_DIR))
import network as model

# DEFAULT SETTINGS
parser = argparse.ArgumentParser()
parser.add_argument('--gpu', type=int, default=0, help='GPU to use [default: GPU 0]')
parser.add_argument('--batch', type=int, default=32, help='Batch Size during training [default: 32]')
parser.add_argument('--epoch', type=int, default=200, help='Epoch to run [default: 200]')
parser.add_argument('--output_dir', type=str, default='train_results', help='Directory that stores all training logs and trained models')
parser.add_argument('--wd', type=float, default=0, help='Weight Decay [Default: 0.0]')
FLAGS = parser.parse_args()

hdf5_data_dir = os.path.join(BASE_DIR, './hdf5_data')

# MAIN SCRIPT
batch_size = FLAGS.batch
output_dir = FLAGS.output_dir

if not os.path.exists(output_dir):
    os.mkdir(output_dir)

print('#### Batch Size: {0}'.format(batch_size))
print('#### Training using GPU: {0}'.format(FLAGS.gpu))

LEARNING_RATE = 1e-4
TRAINING_EPOCHES = FLAGS.epoch
print('### Training epoch: {0}'.format(TRAINING_EPOCHES))

# def get_file_name(file_path):
#     parts = file_path.split('/')
#     part = parts[-1]
#     parts = part.split('.')
#     return parts[0]
# TRAINING_FILE_LIST = [get_file_name(file_name) for file_name in glob.glob('../data/ShapeNet/train/' + '*.mat')]

def getDataFiles(list_filename):
    return [line.rstrip() for line in open(list_filename)]

TRAINING_FILE_LIST = provider.getDataFiles(os.path.join(hdf5_data_dir, 'train_hdf5_file_list.txt'))


MODEL_STORAGE_PATH = os.path.join(output_dir, 'trained_models')
if not os.path.exists(MODEL_STORAGE_PATH):
    os.mkdir(MODEL_STORAGE_PATH)

LOG_STORAGE_PATH = os.path.join(output_dir, 'logs')
if not os.path.exists(LOG_STORAGE_PATH):
    os.mkdir(LOG_STORAGE_PATH)

SUMMARIES_FOLDER =  os.path.join(output_dir, 'summaries')
if not os.path.exists(SUMMARIES_FOLDER):
    os.mkdir(SUMMARIES_FOLDER)

def printout(flog, data):
    print(data)
    flog.write(data + '\n')

def load_and_enqueue(sess, enqueue_op, pointgrid_ph, cat_label_ph, seg_label_ph):
    for epoch in range(1000 * TRAINING_EPOCHES):
        train_file_idx = np.arange(0, len(TRAINING_FILE_LIST))
        np.random.shuffle(train_file_idx)
        for loop in range(0, len(TRAINING_FILE_LIST)):
            # mat_content = scipy.io.loadmat('../data/ShapeNet/train/' + TRAINING_FILE_LIST[train_file_idx[loop]] + '.mat')
            # pc = mat_content['points']
            # labels = np.squeeze(mat_content['labels'])
            # category = mat_content['category'][0][0]
            cur_train_filename = os.path.join(hdf5_data_dir, TRAINING_FILE_LIST[train_file_idx[loop]])
            print('Loading train file ' + cur_train_filename)

            cur_data, cur_labels, cur_seg = provider.loadDataFile_with_seg(cur_train_filename)
            cur_data, cur_labels, order = provider.shuffle_data(cur_data, np.squeeze(cur_labels))
            cur_seg = cur_seg[order, ...]
            
            for pc_idx in range(0, len(cur_data)):
            	pc = cur_data[pc_idx]
            	category = cur_labels[pc_idx]
            	labels = cur_seg[pc_idx]

                pc = model.rotate_pc(pc)
                cat_label = model.integer_label_to_one_hot_label(category)
                seg_label = model.integer_label_to_one_hot_label(labels)
                pointgrid, pointgrid_label, _ = model.pc2voxel(pc, seg_label)
                sess.run(enqueue_op, feed_dict={pointgrid_ph: pointgrid, cat_label_ph: cat_label, seg_label_ph: pointgrid_label})

def placeholder_inputs():
    pointgrid_ph = tf.placeholder(tf.float32, shape=(model.N, model.N, model.N, model.NUM_FEATURES))
    cat_label_ph = tf.placeholder(tf.float32, shape=(model.NUM_CATEGORY))
    seg_label_ph = tf.placeholder(tf.float32, shape=(model.N, model.N, model.N, model.K+1, model.NUM_SEG_PART))
    return pointgrid_ph, cat_label_ph, seg_label_ph

def load_checkpoint(checkpoint_dir, session, var_list=None):
    print(' [*] Loading checkpoint...')
    ckpt = tf.train.get_checkpoint_state(checkpoint_dir)
    if ckpt and ckpt.model_checkpoint_path:
        ckpt_name = os.path.basename(ckpt.model_checkpoint_path)
        ckpt_path = os.path.join(checkpoint_dir, ckpt_name)
    try:
        restorer = tf.train.Saver(var_list)
        restorer.restore(session, ckpt_path)
        print(' [*] Loading successful! Copy variables from % s' % ckpt_path)
        return True
    except:
        print(' [*] No suitable checkpoint!')
        return False

class StoppableThread(threading.Thread):
    """Thread class with a stop() method. The thread itself has to check
    regularly for the stopped() condition."""

    def __init__(self, target=None, args=None):
        super(StoppableThread, self).__init__(target=target, args=args)
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def stopped(self):
        return self._stop_event.is_set()

def train():
    with tf.Graph().as_default():
        with tf.device('/gpu:'+str(FLAGS.gpu)):
            pointgrid_ph, cat_label_ph, seg_label_ph = placeholder_inputs()
            is_training_ph = tf.placeholder(tf.bool, shape=())

            queue = tf.FIFOQueue(capacity=20*batch_size, dtypes=[tf.float32, tf.float32, tf.float32],\
                                                         shapes=[[model.N, model.N, model.N, model.NUM_FEATURES],\
                                                                 [model.NUM_CATEGORY],
                                                                 [model.N, model.N, model.N, model.K+1, model.NUM_SEG_PART]])
            enqueue_op = queue.enqueue([pointgrid_ph, cat_label_ph, seg_label_ph])
            dequeue_pointgrid, dequeue_cat_label, dequeue_seg_label = queue.dequeue_many(batch_size)

            # model
            pred_cat, pred_seg = model.get_model(dequeue_pointgrid, is_training=is_training_ph)

            # loss
            total_loss, cat_loss, seg_loss = model.get_loss(pred_cat, dequeue_cat_label, pred_seg, dequeue_seg_label)

            # optimization
            total_var = tf.trainable_variables()
            step = tf.train.AdamOptimizer(learning_rate=LEARNING_RATE).minimize(total_loss, var_list=total_var)

        # write logs to the disk
        flog = open(os.path.join(LOG_STORAGE_PATH, 'log.txt'), 'w')

        saver = tf.train.Saver()

        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        config.allow_soft_placement = True
        sess = tf.Session(config=config)

        ckpt_dir = './train_results/trained_models'
        if not load_checkpoint(ckpt_dir, sess):
            sess.run(tf.global_variables_initializer())

        train_writer = tf.summary.FileWriter(SUMMARIES_FOLDER + '/train', sess.graph)
        test_writer = tf.summary.FileWriter(SUMMARIES_FOLDER + '/test')

        fcmd = open(os.path.join(LOG_STORAGE_PATH, 'cmd.txt'), 'w')
        fcmd.write(str(FLAGS))
        fcmd.close()

        def train_one_epoch(epoch_num):
            is_training = True

            num_data = len(TRAINING_FILE_LIST)
            num_batch = num_data // batch_size
            total_loss_acc = 0.0
            cat_loss_acc = 0.0
            seg_loss_acc = 0.0
            display_mark = max([num_batch // 4, 1])
            for i in range(num_batch):
                _, total_loss_val, cat_loss_val, seg_loss_val = sess.run([step, total_loss, cat_loss, seg_loss], feed_dict={is_training_ph: is_training})
                total_loss_acc += total_loss_val
                cat_loss_acc += cat_loss_val
                seg_loss_acc += seg_loss_val

                if ((i+1) % display_mark == 0):
                    printout(flog, 'Epoch %d/%d - Iter %d/%d' % (epoch_num+1, TRAINING_EPOCHES, i+1, num_batch))
                    printout(flog, 'Total Loss: %f' % (total_loss_acc / (i+1)))
                    printout(flog, 'Classification Loss: %f' % (cat_loss_acc / (i+1)))
                    printout(flog, 'Segmentation Loss: %f' % (seg_loss_acc / (i+1)))

            printout(flog, '\tMean Total Loss: %f' % (total_loss_acc / num_batch))
            printout(flog, '\tMean Classification Loss: %f' % (cat_loss_acc / num_batch))
            printout(flog, '\tMean Segmentation Loss: %f' % (seg_loss_acc / num_batch))

        if not os.path.exists(MODEL_STORAGE_PATH):
            os.mkdir(MODEL_STORAGE_PATH)

        coord = tf.train.Coordinator()
        for num_thread in range(16):
            t = StoppableThread(target=load_and_enqueue, args=(sess, enqueue_op, pointgrid_ph, cat_label_ph, seg_label_ph))
            t.setDaemon(True)
            t.start()
            coord.register_thread(t)

        for epoch in range(TRAINING_EPOCHES):
            printout(flog, '\n>>> Training for the epoch %d/%d ...' % (epoch+1, TRAINING_EPOCHES))

            train_one_epoch(epoch)

            if (epoch+1) % 1 == 0:
                cp_filename = saver.save(sess, os.path.join(MODEL_STORAGE_PATH, 'epoch_' + str(epoch+1)+'.ckpt'))
                printout(flog, 'Successfully store the checkpoint model into ' + cp_filename)

            flog.flush()
        flog.close()

if __name__=='__main__':
    train()
