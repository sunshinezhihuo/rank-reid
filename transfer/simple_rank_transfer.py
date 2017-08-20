import os

from keras import Input
from keras.layers import Flatten, Lambda, Dense

from pretrain.eval import grid_test_rank_eval

os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"  # see issue #152
os.environ["CUDA_VISIBLE_DEVICES"] = "1"
import numpy as np
from keras.callbacks import EarlyStopping, ReduceLROnPlateau
from keras.engine import Model
from keras.preprocessing import image
from keras.applications.resnet50 import preprocess_input
from keras.models import load_model
from keras.utils import plot_model
from numpy.random import randint
from keras import backend as K


def reid_img_prepare(LIST, TRAIN):
    images = []
    with open(LIST, 'r') as f:
        for line in f:
            if 'jp' not in line:
                continue
            line = line.strip()
            img = line.split()[0]
            img = image.load_img(os.path.join(TRAIN, img), target_size=[224, 224])
            img = image.img_to_array(img)
            img = np.expand_dims(img, axis=0)
            img = preprocess_input(img)
            images.append(img[0])
    images = np.array(images)
    return images


def gen_neg_right_img_ids(left_similar_persons, left_similar_matrix, batch_size):
    right_img_ids = list()
    right_img_idxes = randint(25, 50, size=batch_size)
    right_img_scores = list()
    for i in range(batch_size):
        right_img_ids.append(left_similar_persons[i][right_img_idxes[i]])
        right_img_scores.append(left_similar_matrix[i][right_img_idxes[i]])
    right_img_ids = np.array(right_img_ids)
    return right_img_ids, np.array(right_img_scores)


def gen_pos_right_img_ids(left_similar_persons, left_similar_matrix, batch_size):
    right_img_ids = list()
    right_img_idxes = randint(0, 25, size=batch_size)
    right_img_scores = list()
    for i in range(batch_size):
        right_img_ids.append(left_similar_persons[i][right_img_idxes[i]])
        right_img_scores.append(left_similar_matrix[i][right_img_idxes[i]])
    right_img_ids = np.array(right_img_ids)
    return right_img_ids, np.array(right_img_scores)


def gen_right_img_infos(cur_epoch, similar_matrix, similar_persons, left_img_ids, img_cnt, batch_size):
    pos_prop = 2
    if cur_epoch % pos_prop == 0:
        # select from last match for negative
        left_similar_persons = similar_persons[left_img_ids]
        left_similar_matrix = similar_matrix[left_img_ids]
        right_img_ids, right_img_scores = gen_pos_right_img_ids(left_similar_persons, left_similar_matrix,batch_size)
    else:
        # select from last match for negative
        left_similar_persons = similar_persons[left_img_ids]
        left_similar_matrix = similar_matrix[left_img_ids]
        right_img_ids, right_img_scores = gen_neg_right_img_ids(left_similar_persons, left_similar_matrix, batch_size)
    right_img_ids = right_img_ids.astype(int)
    return right_img_ids, right_img_scores


def triplet_generator_by_rank_list(train_images, batch_size, similar_persons, similar_matrix, train=False):
    cur_epoch = 0

    img_cnt = len(similar_persons)

    while True:
        left_img_ids = randint(img_cnt, size=batch_size)
        right_img_ids1, right_img_scores1 = gen_right_img_infos(cur_epoch,
                                                           similar_matrix, similar_persons,
                                                           left_img_ids,
                                                           img_cnt, batch_size)
        cur_epoch += 1
        right_img_ids2, right_img_scores2 = gen_right_img_infos(cur_epoch,
                                                           similar_matrix, similar_persons,
                                                           left_img_ids,
                                                           img_cnt, batch_size)
        left_images = train_images[left_img_ids]
        right_images1 = train_images[right_img_ids1]
        right_images2 = train_images[right_img_ids2]
        sub_scores = np.subtract(right_img_scores1, right_img_scores2)
        cur_epoch += 1
        if cur_epoch % 2 == 0:
            yield [left_images, right_images1, right_images2], [sub_scores]
        else:
            yield [left_images, right_images2, right_images1], [-sub_scores]

def eucl_dist(inputs):
    x, y = inputs
    return (x - y) ** 2


def sub(inputs):
    x, y = inputs
    return x - y


def cross_entropy_loss(predict_score, real_score):
    predict_prob = 1 / (1 + K.exp(-predict_score))
    real_prob = 1 / (1 + K.exp(-real_score))
    cross_entropy = -real_prob * K.log(predict_prob) - (1 - real_prob) * K.log(1 - predict_prob)
    return cross_entropy


def rank_transfer_model(pair_model_path):
    base_model = load_model(pair_model_path).layers[2]
    base_model = Model(inputs=base_model.get_input_at(0), outputs=[base_model.get_output_at(0)], name='resnet50')
    img0 = Input(shape=(224, 224, 3), name='img_0')
    img1 = Input(shape=(224, 224, 3), name='img_1')
    img2 = Input(shape=(224, 224, 3), name='img_2')
    feature0 = Flatten()(base_model(img0))
    feature1 = Flatten()(base_model(img1))
    feature2 = Flatten()(base_model(img2))
    dis1 = Lambda(eucl_dist, name='square1')([feature0, feature1])
    dis2 = Lambda(eucl_dist, name='square2')([feature0, feature2])
    score1 = Dense(1, activation='linear', name='score1')(dis1)
    score2 = Dense(1, activation='linear', name='score2')(dis2)
    sub_score = Lambda(sub, name='sub_score')([score1, score2])

    model = Model(inputs=[img0, img1, img2], outputs=[sub_score])
    plot_model(model, to_file='rank_model.png')
    for layer in base_model.layers[:-10]:
        layer.trainable = False
    for layer in base_model.layers[-10:]:
        layer.trainable = True
    return model


def rank_transfer(train_generator, val_generator, source_model_path, batch_size=48):
    model = rank_transfer_model(source_model_path)
    plot_model(model, 'rank_model.png')
    model.compile(optimizer='nadam',
                  loss={
                      'sub_score': cross_entropy_loss
                      # 'sub_score': 'mse',
                    },
                  loss_weights={
                      'sub_score': 1.
                  },
                  # metrics=['accuracy']
                  )

    early_stopping = EarlyStopping(monitor='val_loss', patience=1)
    auto_lr = ReduceLROnPlateau(monitor='val_loss', factor=0.1, patience=3, verbose=0, mode='auto', epsilon=0.0001,
                                cooldown=0, min_lr=0)
    model.fit_generator(train_generator,
                        steps_per_epoch=16500 / batch_size + 1,
                        epochs=30,
                        validation_data=val_generator,
                        validation_steps=1800 / batch_size + 1,
                        callbacks=[early_stopping, auto_lr])
    model.save('simple_rank_transfer.h5')


def rank_transfer_2market():
    DATASET = '../dataset/Market'
    LIST = os.path.join(DATASET, 'pretrain.list')
    TRAIN = os.path.join(DATASET, 'bounding_box_train')
    train_images = reid_img_prepare(LIST, TRAIN)
    batch_size = 64
    similar_persons = np.genfromtxt('../pretrain/train_renew_pid.log', delimiter=' ')
    similar_matrix = np.genfromtxt('../pretrain/train_renew_ac.log', delimiter=' ')
    rank_transfer(
        triplet_generator_by_rank_list(train_images, batch_size, similar_persons, similar_matrix, train=True),
        triplet_generator_by_rank_list(train_images, batch_size, similar_persons, similar_matrix, train=False),
        '../pretrain/pair_pretrain.h5',
        batch_size=batch_size
    )


def rank_transfer_2grid():
    DATASET = '/home/cwh/coding/grid_train_probe_gallery/cross0'
    LIST = os.path.join(DATASET, 'pretrain/test_track.txt')
    TRAIN = os.path.join(DATASET, 'pretrain')
    train_images = reid_img_prepare(LIST, TRAIN)
    batch_size = 64
    similar_persons = np.genfromtxt('../pretrain/grid_cross0/train_renew_pid.log', delimiter=' ')
    similar_matrix = np.genfromtxt('../pretrain/grid_cross0/train_renew_ac.log', delimiter=' ')
    rank_transfer(
        triplet_generator_by_rank_list(train_images, batch_size, similar_persons, similar_matrix, train=True),
        triplet_generator_by_rank_list(train_images, batch_size, similar_persons, similar_matrix, train=False),
        '../pretrain/pair_pretrain.h5',
        batch_size=batch_size
    )

if __name__ == '__main__':
    rank_transfer_2grid()
    grid_test_rank_eval('../transfer/simple_rank_transfer.h5', 'grid_cross0_simple_srank_transfer')
    # [0.208, 0.336, 0.4, 0.464, 0.624]