from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import tensorflow as tf
import keras
from keras import backend as K
from keras import layers
from keras.engine import InputSpec, Layer
from keras import initializers
from keras.callbacks import ModelCheckpoint
import os
from os import path

import data
from resources_out import RES_OUT_DIR
from resources import LATEST_KERAS_WEIGHTS
from toxicity_classifier.metrics import calc_f1, calc_precision, calc_recall, RocCallback


class AttentionWeightedAverage(Layer):
    """
    Computes a weighted average of the different channels across timesteps.
    Uses 1 parameter pr. channel to compute the attention value for a single timestep.
    """

    def __init__(self, return_attention=False, **kwargs):
        self.init = initializers.get('uniform')
        self.supports_masking = True
        self.return_attention = return_attention
        super(AttentionWeightedAverage, self).__init__(**kwargs)

    def build(self, input_shape):
        self.input_spec = [InputSpec(ndim=3)]
        assert len(input_shape) == 3

        self.atten_weights = self.add_weight(shape=(input_shape[2], 1),
                                             name='{}_atten_weights'.format(self.name),
                                             initializer=self.init)
        self.trainable_weights = [self.atten_weights]
        super(AttentionWeightedAverage, self).build(input_shape)

    def call(self, inputs, **kwargs):
        # computes a probability distribution over the timesteps
        # uses 'max trick' for numerical stability
        # reshape is done to avoid issue with Tensorflow
        # and 1-dimensional weights
        mask = None
        for key, value in kwargs.items():
            if key == "mask":
                mask = value

        logits = K.dot(inputs, self.atten_weights)
        x_shape = K.shape(inputs)
        logits = K.reshape(logits, (x_shape[0], x_shape[1]))
        ai = K.exp(logits - K.max(logits, axis=-1, keepdims=True))

        # masked timesteps have zero weight
        if mask is not None:
            mask = K.cast(mask, K.floatx())
            ai = ai * mask
        att_weights = ai / (K.sum(ai, axis=1, keepdims=True) + K.epsilon())
        weighted_input = inputs * K.expand_dims(att_weights)
        result = K.sum(weighted_input, axis=1)
        return [result, att_weights]

    def get_output_shape(self, input_shape):
        return self.compute_output_shape(input_shape)

    def compute_output_shape(self, input_shape):
        output_len = input_shape[2]
        return [(input_shape[0], output_len), (input_shape[0], input_shape[1])]  # [atten_weighted_sum, atten_weights]


class CustomLoss(object):
    @staticmethod
    def binary_crossentropy_with_bias(train_labels_1_ratio, train_on_toxic_only=False):
        train_labels_0_ratio = 1 - train_labels_1_ratio
        train_labels_1_bias = 1 / train_labels_1_ratio
        train_labels_0_bias = 1 / train_labels_0_ratio
        train_labels_normalizer = train_labels_0_bias * train_labels_1_bias
        train_labels_0_bias = train_labels_0_bias / train_labels_normalizer
        train_labels_1_bias = train_labels_1_bias / train_labels_normalizer

        def loss_function(y_true, y_pred):
            if train_on_toxic_only:
                return K.mean(train_labels_1_bias[0] * K.binary_crossentropy(y_true[:, 0], y_pred[:, 0]),
                              axis=-1) + K.mean(
                    train_labels_0_bias[0] * K.binary_crossentropy(1 - y_true[:, 0], 1 - y_pred[:, 0]), axis=-1)
            else:
                return K.mean(train_labels_1_bias * K.binary_crossentropy(y_true, y_pred), axis=-1) + K.mean(
                    train_labels_0_bias * K.binary_crossentropy(1 - y_true, 1 - y_pred), axis=-1)

        return loss_function


class ToxClassifierConfig(object):
    # pylint: disable = too-many-arguments
    def __init__(self,
                 restore=True,
                 restore_path=LATEST_KERAS_WEIGHTS,
                 checkpoint=False,
                 checkpoint_path=RES_OUT_DIR,
                 use_gpu=tf.test.is_gpu_available(),
                 train_labels_1_ratio=data.Dataset.init_embedding_from_dump()[2],
                 run_name='',
                 train_on_toxic_only=False,
                 debug=True):
        self.restore = restore
        self.restore_path = restore_path
        self.checkpoint = checkpoint
        self.checkpoint_path = checkpoint_path
        self.use_gpu = use_gpu
        self.train_labels_1_ratio = train_labels_1_ratio
        self.run_name = run_name
        self.train_on_toxic_only = train_on_toxic_only
        self.debug = debug


class ToxicityClassifier(object):
    # pylint: disable = too-many-arguments
    def __init__(self, session, max_seq=500, embedding_matrix=None, config=None):
        # type: (tf.Session, np.int, np.ndarray, ToxClassifierConfig) -> None

        if embedding_matrix is None:
            embedding_matrix = data.Dataset.init_embedding_from_dump()[0]
        self._embedding = embedding_matrix
        self._num_tokens = embedding_matrix.shape[0]
        self._embed_dim = embedding_matrix.shape[1]
        self._input_layer = None
        self._output_layer = None
        self._atten_w = None
        self._metrics = ['accuracy', 'ce', calc_precision, calc_recall, calc_f1]
        self.grad_fn = None
        self._session = session
        self._max_seq = max_seq
        self._config = config if config else ToxClassifierConfig()
        self._model = self._build_graph()  # type: keras.Model

    # LAYERS ------------------------------------------------------------------------

    def embedding_layer(self, tensor):
        emb = layers.Embedding(input_dim=self._num_tokens, output_dim=self._embed_dim, input_length=self._max_seq,
                               trainable=False, mask_zero=False, weights=[self._embedding])
        return emb(tensor)

    def spatial_dropout_layer(self, tensor, rate=0.25):
        dropout = layers.SpatialDropout1D(rate=rate)
        return dropout(tensor)

    def dropout_layer(self, tensor, rate=0.7):
        dropout = layers.Dropout(rate=rate)
        return dropout(tensor)

    def bidirectional_rnn(self, tensor, amount=60):
        if self._config.use_gpu:
            bi_rnn = layers.Bidirectional(layers.CuDNNGRU(amount, return_sequences=True))
        else:
            bi_rnn = layers.Bidirectional(
                layers.GRU(amount, return_sequences=True, reset_after=True, recurrent_activation='sigmoid'))
        return bi_rnn(tensor)

    def concat_layer(self, tensors, axis):
        return layers.concatenate(tensors, axis=axis)

    def mask_tensor(self, tensor):
        zeros = K.zeros_like(tensor[:, :, 0], dtype=np.int32)
        bool_mask = K.not_equal(zeros, self._input_layer)
        bool_mask_float = tf.cast(bool_mask, np.float32)
        bool_mask_float = K.tile(K.expand_dims(bool_mask_float), n=(1, 1, 240))
        return tf.multiply(tensor, bool_mask_float)

    def mask_seq(self, tensor):
        mask = layers.Lambda(self.mask_tensor)
        return mask(tensor)

    def last_stage(self, tensor):
        last = layers.Lambda(lambda t: t[:, -1], name='last')
        return last(tensor)

    def max_polling_layer(self, tensor):
        maxpool = layers.GlobalMaxPooling1D()
        return maxpool(tensor)

    def avg_polling_layer(self, tensor):
        avgpool = layers.GlobalAveragePooling1D()
        return avgpool(tensor)

    def attention_layer(self, tensor):
        attenion = AttentionWeightedAverage()
        atten, atten_w = attenion(tensor)
        return atten, atten_w

    def dense_layer(self, tensor, out_size=144):
        dense = layers.Dense(out_size, activation='relu')
        return dense(tensor)

    def output_layer(self, tensor, out_size=6):
        output = layers.Dense(out_size, activation='sigmoid')
        return output(tensor)

    # GRAPH ------------------------------------------------------------------------

    def _build_graph(self):
        K.set_session(self._session)

        # embed:
        self._input_layer = keras.Input(shape=(self._max_seq,), dtype='int32')
        self._embedding = self.embedding_layer(self._input_layer)
        dropout1 = self.spatial_dropout_layer(self._embedding)

        # rnn:
        rnn1 = self.bidirectional_rnn(dropout1)
        rnn2 = self.bidirectional_rnn(rnn1)
        concat = self.concat_layer([rnn1, rnn2], axis=2)
        mask = self.mask_seq(concat)
        # attentions:
        avgpool = self.avg_polling_layer(mask)
        maxpool = self.max_polling_layer(mask)
        last_stage = self.last_stage(mask)
        atten, self._atten_w = self.attention_layer(mask)
        all_views = self.concat_layer([last_stage, maxpool, avgpool, atten], axis=1)

        # classify:
        dropout2 = self.dropout_layer(all_views)
        dense = self.dense_layer(dropout2)
        if self._config.train_on_toxic_only:
            self._output_layer = self.output_layer(dense, out_size=1)
        else:
            self._output_layer = self.output_layer(dense)

        model = keras.Model(inputs=self._input_layer, outputs=self._output_layer)
        adam_optimizer = keras.optimizers.Adam(lr=1e-3, decay=1e-6, clipvalue=5)

        # restore:
        if self._config.restore:
            saved = self._config.restore_path
            assert path.exists(saved), 'Saved model was not found'
            model.load_weights(saved)
            print("Restoring weights from " + saved)

        model.compile(
            loss=CustomLoss.binary_crossentropy_with_bias(self._config.train_labels_1_ratio,
                                                          self._config.train_on_toxic_only),
            optimizer=adam_optimizer,
            metrics=self._metrics)

        if self._config.debug:
            model.summary()
        return model

    def _define_callbacks(self):
        callback_list = list()
        if self._config.checkpoint:
            save_path = self._config.checkpoint_path
            if not os.path.isdir(save_path):
                os.mkdir(save_path)
            file_name = self._config.run_name + "_weights-epoch-{epoch:02d}-val_f1-{val_calc_f1:.2f}.hdf5"
            file_path = path.join(save_path, file_name)
            checkpoint = ModelCheckpoint(file_path, monitor='val_calc_f1', verbose=1, save_best_only=True,
                                         mode='max')
            callback_list.append(checkpoint)
        return callback_list

    # TRAIN ------------------------------------------------------------------------

    def train(self, dataset):
        # type: (data.Dataset) -> keras.callbacks.History
        callback_list = self._define_callbacks()
        callback_list.append(RocCallback(dataset))
        if self._config.train_on_toxic_only:
            history = self._model.fit(x=dataset.train_seq[:, :], y=dataset.train_lbl[:, 0], batch_size=500,
                                      validation_data=(dataset.val_seq[:, :], dataset.val_lbl[:, 0]), epochs=50,
                                      callbacks=callback_list)
        else:
            history = self._model.fit(x=dataset.train_seq[:, :], y=dataset.train_lbl[:, :], batch_size=500,
                                      validation_data=(dataset.val_seq[:, :], dataset.val_lbl[:, :]), epochs=50,
                                      callbacks=callback_list)
        return history

    # INFER ------------------------------------------------------------------------

    def classify(self, seq):
        # type: (np.ndarray) -> np.ndarray
        prediction = self._model.predict(seq)
        return prediction

    def get_grad_fn(self):
        # only interested in the first label
        grad_0 = K.gradients(loss=self._model.output[:, 0], variables=self._embedding)[0]
        grads = [grad_0]
        fn = K.function(inputs=[self._model.input], outputs=grads)
        return fn

    def get_gradient(self, seq):
        self.grad_fn = self.get_grad_fn() if self.grad_fn == None else self.grad_fn
        return self.grad_fn([seq])[0]

    def get_attention(self, seq):
        fn = K.function(inputs=[self._model.input], outputs=[self._atten_w])
        return fn([seq])[0]

    def get_attention_fn(self):
        fn = K.function(inputs=[self._model.input], outputs=[self._atten_w])
        return fn
