import lasagne
import theano
import theano.tensor as T
import numpy as np
from collections import OrderedDict
try:
    from pastalog import Log
    force_not_log = False
except:
    force_not_log = True
from datetime import datetime as dt
import logging
import settings
import os
import utils
import matplotlib.pylab as plt
import boltons
import skimage.transform
import skimage.filters
import skimage.color
import scipy
from sklearn.manifold import TSNE
from sklearn.preprocess import normalize


logger = logging.getLogger('Ghiaseddin')
hdlr = logging.FileHandler('/tmp/Ghiaseddin.log')
formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
hdlr.setFormatter(formatter)
logger.addHandler(hdlr)
logger.setLevel(logging.DEBUG)


class Ghiaseddin(object):
    _epsilon = 1.0e-7
    log_step = 0

    def __init__(self, extractor, dataset, train_batch_size=16, extractor_learning_rate=1e-5, ranker_learning_rate=1e-4,
                 weight_decay=1e-5, optimizer=lasagne.updates.rmsprop, ranker_nonlinearity=lasagne.nonlinearities.linear, debug=False,
                 do_log=True):

        self.train_batch_size = train_batch_size
        self.extractor = extractor
        self.dataset = dataset
        self.weight_decay = weight_decay
        self.optimizer = optimizer
        self.ranker_nonlinearity = ranker_nonlinearity
        self.extractor_learning_rate = extractor_learning_rate
        self.ranker_learning_rate = ranker_learning_rate
        self.debug = debug
        self.do_log = do_log

        if force_not_log:
            self.do_log = False
            logger.warning('Not logging because pastalog is not installed.')

        extractor_name = self.extractor.__class__.__name__
        if extractor.augmentation:
            extractor_name = "%s-aug" % extractor_name

        self.NAME = "e:%s-d:%s-bs:%d-elr:%f-rlr:%f-opt:%s-rnl:%s-wd:%f-rs:%s" % (extractor_name,
                                                                                 self.dataset.get_name(),
                                                                                 self.train_batch_size,
                                                                                 extractor_learning_rate,
                                                                                 ranker_learning_rate,
                                                                                 self.optimizer.__name__,
                                                                                 self.ranker_nonlinearity.__name__,
                                                                                 self.weight_decay,
                                                                                 str(settings.RANDOM_SEED))
        if self.do_log:
            self.pastalog = Log('http://localhost:8100/', self.NAME)

        # TODO: check if converting these to shared variable actually improves
        # performance.
        self.input_var = T.ftensor4('inputs')
        self.target_var = T.fvector('targets')

        self.extractor.set_input_var(
            self.input_var, batch_size=train_batch_size)
        self.extractor_layer = self.extractor.get_output_layer()

        self.extractor_learning_rate_shared_var = theano.shared(
            np.cast['float32'](extractor_learning_rate), name='extractor_learning_rate')
        self.ranker_learning_rate_shared_var = theano.shared(
            np.cast['float32'](ranker_learning_rate), name='ranker_learning_rate')

        self.extractor_params = lasagne.layers.get_all_params(
            self.extractor_layer, trainable=True)

        self.absolute_rank_estimate, self.ranker_params = self._create_absolute_rank_estimate(
            self.extractor_layer)
        self.reshaped_input = lasagne.layers.ReshapeLayer(
            self.absolute_rank_estimate, (-1, 2))

        # the posterior estimate layer is not trainable
        self.posterior_estimate = lasagne.layers.DenseLayer(self.reshaped_input, num_units=1, W=lasagne.init.np.array(
            [[1], [-1]]), b=lasagne.init.Constant(val=0), nonlinearity=lasagne.nonlinearities.sigmoid)
        self.posterior_estimate.params[
            self.posterior_estimate.W].remove('trainable')
        self.posterior_estimate.params[
            self.posterior_estimate.b].remove('trainable')

        # the clipping is done to prevent the model from diverging as caused by
        # binary XEnt
        self.predictions = T.clip(lasagne.layers.get_output(
            self.posterior_estimate).ravel(), self._epsilon, 1.0 - self._epsilon)

        self.xent_loss = lasagne.objectives.binary_crossentropy(
            self.predictions, self.target_var).mean()
        self.l2_penalty = lasagne.regularization.regularize_network_params(
            self.absolute_rank_estimate, lasagne.regularization.l2)
        self.loss = self.xent_loss + self.l2_penalty * self.weight_decay

        self.test_absolute_rank_estimate = lasagne.layers.get_output(
            self.absolute_rank_estimate, deterministic=True)

        self._create_theano_functions()

    def _create_theano_functions(self):
        """
        Will be creating theano functions for training and testing
        """
        if self.extractor_learning_rate != 0:
            self._feature_extractor_updates = self.optimizer(
                self.loss, self.extractor_params, learning_rate=self.extractor_learning_rate_shared_var)
        else:
            self._feature_extractor_updates = OrderedDict()

        if self.ranker_learning_rate != 0:
            self._ranker_updates = self.optimizer(
                self.loss, self.ranker_params, learning_rate=self.ranker_learning_rate_shared_var)
        else:
            self._ranker_updates = OrderedDict()

        f = self._feature_extractor_updates.items()
        r = self._ranker_updates.items()
        f.extend(r)

        self._all_updates = OrderedDict(f)

        self.training_function = theano.function([self.input_var, self.target_var], [
                                                 self.loss, self.xent_loss, self.l2_penalty], updates=self._all_updates)
        self.testing_function = theano.function(
            [self.input_var], self.test_absolute_rank_estimate)

    def _create_absolute_rank_estimate(self, incoming):
        """
        An abstraction around the absolute rank estimate.
        Currently this is only a single dense layer with linear activation. This could easily be extended with more non-linearity.
        Should return the absolute rank estimate layer and all the parameters.
        """
        absolute_rank_estimate_layer = lasagne.layers.DenseLayer(incoming=incoming, num_units=1, W=lasagne.init.GlorotUniform(
        ), b=lasagne.init.Constant(val=0.1), nonlinearity=lasagne.nonlinearities.linear)
        # Take care if you want to use multiple layers here you have to return
        # all the params of all the layers
        return absolute_rank_estimate_layer, absolute_rank_estimate_layer.get_params()

    def _train_1_batch(self, preprocessed_input):
        tic = dt.now()
        input_data, input_target, input_mask = preprocessed_input
        loss, xent_loss, l2_penalty = self.training_function(
            input_data, input_target)

        # log the losses
        if not np.isnan(loss):
            if self.do_log:
                self.pastalog.post('train_loss', value=float(loss), step=self.log_step)
                self.pastalog.post('train_xent', value=float(
                    xent_loss), step=self.log_step)
                self.pastalog.post('train_l2pen', value=float(
                    l2_penalty), step=self.log_step)
        else:
            logger.warning('nan loss')

        toc = dt.now()

        self.log_step += 1
        if self.debug:
            logger.debug("%d minibatch took: %s" % (self.log_step, str(toc - tic)))
        return loss

    def train_one_epoch(self):
        tic = dt.now()
        train_generator = self.dataset.train_generator(
            batch_size=self.train_batch_size, shuffle=True, cut_tail=True)
        losses = []
        for i, b in enumerate(train_generator):
            preprocessed_input = self.extractor.preprocess(b, self.extractor.augmentation)
            batch_loss = self._train_1_batch(preprocessed_input)
            losses.append(batch_loss)
        toc = dt.now()

        if self.debug:
            logger.info("Training for 1 epoch took: %s", str(toc - tic))
        return losses

    def train_n_epoch(self, n):
        for _ in range(n):
            self.train_one_epoch()

    def train_n_iter(self, n):
        losses = []
        current_iter = 0
        total_epochs = 0
        finished = False
        while True and not finished:
            train_generator = self.dataset.train_generator(
                batch_size=self.train_batch_size, shuffle=True, cut_tail=True)
            for i, b in enumerate(train_generator):
                preprocessed_input = self.extractor.preprocess(b, self.extractor.augmentation)
                batch_loss = self._train_1_batch(preprocessed_input)
                losses.append(batch_loss)
                current_iter += 1
                if current_iter >= n:
                    finished = True
                    break
            if not finished:
                total_epochs += 1

        return losses, total_epochs

    def _test_rank_estimate(self, preprocessed_input):
        input_data, input_target, input_mask = preprocessed_input
        rank_estimates = self.testing_function(input_data)

        return rank_estimates, input_target, input_mask

    @staticmethod
    def _estimates_to_target_estimates(estimates):
        assert len(estimates) % 2 == 0
        o1 = estimates[::2]
        o2 = estimates[1::2]
        posteriors = np.zeros_like(o1)
        posteriors += (o1 == o2) * 0.5 + (o1 > o2) * 1
        return posteriors.ravel()

    def eval_accuracy(self):
        tic = dt.now()
        test_generator = self.dataset.test_generator(
            batch_size=self.train_batch_size * 4)
        total = 0
        correct = 0

        for batch in test_generator:
            preprocessed_input = self.extractor.preprocess(batch)
            estimates, target, mask = self._test_rank_estimate(
                preprocessed_input)

            estimated_target = self._estimates_to_target_estimates(estimates)
            # TODO: this for loop could be speeded up with array operations
            # instead of for
            for p, t, m in zip(estimated_target, target, mask):
                if m and t != 0.5:
                    total += 1
                    if t == p:
                        correct += 1
        toc = dt.now()

        if self.debug:
            logger.info("Evaluation took: %s", str(toc - tic))
        return float(correct) / total

    def _model_name_with_iter(self):
        return "%s-iter:%d" % (self.NAME, self.log_step)

    def _model_name_from_settings(self):
        return os.path.join(settings.model_root, "%s.npz" % (self._model_name_with_iter()))

    def save(self, path=None):
        """
        Save the model to file
        TODO: save all parameters not only the network parameters, for easy resuming of training.
        """
        if not path:
            path = self._model_name_from_settings()

        np.savez(path, params=lasagne.layers.get_all_param_values(
            self.absolute_rank_estimate))

    def load(self, path=None):
        """
        Loads the model which is trained for the most iterations.
        """
        if not path:
            list_of_models = os.listdir(settings.model_root)

            most_iters = 0
            the_better_model = ''

            for model in list_of_models:
                if model.startswith(self.NAME) and model.endswith('.npz'):
                    parts = model.split('-')
                    # the last part is like iter:******.npz, so 5 and -4 makes
                    # sense
                    current_iter = int(parts[-1][5:-4])
                    if current_iter > most_iters:
                        most_iters = current_iter
                        the_better_model = model
            if the_better_model == '':
                raise Exception("No model found")
            path = os.path.join(settings.model_root, the_better_model)
            self.log_step = most_iters

        with np.load(path) as data:
            loaded_from_file = data['params']
        lasagne.layers.set_all_param_values(
            self.absolute_rank_estimate, loaded_from_file)

    def generate_misclassified(self):
        test_generator = self.dataset.test_generator(
            batch_size=self.train_batch_size)

        folder_path = os.path.join(
            settings.result_models_root, "missclassified|%s" % self._model_name_with_iter())
        boltons.fileutils.mkdir_p(folder_path)

        num = 0
        for batch in test_generator:
            preprocessed_input = self.extractor.preprocess(batch)
            estimates, target, mask = self._test_rank_estimate(
                preprocessed_input)

            estimated_target = self._estimates_to_target_estimates(estimates)
            for p, t, m, i in zip(estimated_target, target, mask, batch):
                if m and t != 0.5:
                    if t != p:
                        (img1_path, img2_path), truth = i
                        img1 = utils.load_image(img1_path)
                        img2 = utils.load_image(img2_path)

                        fig = plt.figure(figsize=(10, 5))
                        ax1 = fig.add_subplot(121)
                        ax2 = fig.add_subplot(122)

                        ax1.imshow(img1)
                        ax1.axis('off')
                        ax1.set_title('A')
                        ax2.imshow(img2)
                        ax2.axis('off')
                        ax2.set_title('B')

                        attribute_name = self.dataset._ATT_NAMES[
                            self.dataset.attribute_index]
                        truth_thing = '>' if t == 1 else '<'
                        estimated_thing = '>' if p == 1 else '<'
                        plt.suptitle("Attribute: %s | Truth: %s | Estimated: %s" % (
                            attribute_name, truth_thing, estimated_thing))

                        plt.savefig(os.path.join(folder_path, '%d.png' % num))
                        plt.close()
                        num += 1

    def generate_saliency(self, test_pair_ids=[], size=2):
        # get the id of the test pairs to generate saliency on
        if len(test_pair_ids) == 0:
            length = len(self.dataset._test_targets)
            test_pair_ids = np.random.choice(range(length), size=size)
        else:
            size = len(test_pair_ids)

        if not getattr(self, 'saliency_fn', None):
            # create theano function
            inp = self.extractor.get_input_var()
            outp = lasagne.layers.get_output(self.posterior_estimate, deterministic=True)
            saliency = theano.grad(outp.sum(), wrt=inp)
            self.saliency_fn = theano.function([inp], [saliency])

        # give input to the model and compute saliencies
        saliencies = []
        images = []
        for i in range(size):
            pair = self.dataset._test_pairs[test_pair_ids[i], :]
            img1_path = self.dataset._image_addresses[pair[0]]
            img2_path = self.dataset._image_addresses[pair[1]]
            img1 = utils.load_image(img1_path)
            img2 = utils.load_image(img2_path)

            images.append((img1, img2))

            x = np.zeros((2, 3, self.extractor._input_height, self.extractor._input_width), dtype=np.float32)
            x[0, ...] = self.extractor._general_image_preprocess(img1)
            x[1, ...] = self.extractor._general_image_preprocess(img2)
            saliency = self.saliency_fn(x)[0]

            # unprocess saliency
            new_saliency = [0] * 2
            for i in range(2):
                new_saliency[i] = saliency[i][::-1].transpose(1, 2, 0)

            saliencies.append(new_saliency)

        # show, save and return graph of the figure
        def show_image(ax, img):
            ax.imshow(img)
            ax.axis('off')

        def show_saliency(ax, saliency, img):
            # preprocess saliency
            sal_resize = skimage.transform.resize(saliency.max(axis=-1), img.shape[:2])
            sal_resize = skimage.filters.gaussian(sal_resize, 10)
            sal_resize = np.absolute(sal_resize)
            sal_resize = (sal_resize - sal_resize.min()) / (sal_resize.max() - sal_resize.min() + 1e-5)

            ax.matshow(sal_resize, alpha=1)
            ax.imshow(skimage.color.rgb2gray(img), cmap=plt.cm.gray, alpha=0.5)
            ax.axis('off')

        fig = plt.figure(figsize=(10, 2 * size))
        for i in range(size):
            # show first image
            ax = fig.add_subplot(size, 4, 1 + i * 4)
            show_image(ax, images[i][0])

            # show first saliency map
            ax = fig.add_subplot(size, 4, 2 + i * 4)
            show_saliency(ax, saliencies[i][0], images[i][0])

            # show second image
            ax = fig.add_subplot(size, 4, 3 + i * 4)
            show_image(ax, images[i][1])

            # show second saliency map
            ax = fig.add_subplot(size, 4, 4 + i * 4)
            show_saliency(ax, saliencies[i][1], images[i][1])

        return fig

    def generate_embedding(self, for_all=False, random_seed=None):
        if not random_seed:
            random_seed = settings.RANDOM_SEED

        all_image_paths = self.dataset.all_images(for_all)

        if not getattr(self, 'embedding_fn', None):
            inp = self.extractor.get_input_var()
            embedding = lasagne.layers.get_output(self.extractor_layer, deterministic=True)
            rank = lasagne.layers.get_output(self.absolute_rank_estimate, deterministic=True)
            self.embedding_fn = theano.function([inp], [embedding, rank])

        embeddings = np.zeros((len(all_image_paths), self.extractor.out_layer_dim), dtype=np.float32)
        ranks = np.zeros((len(all_image_paths)), dtype=np.float32)

        idx = 0
        for images in boltons.iterutils.chunked(all_image_paths, self.train_batch_size * 2):
            x = np.zeros((len(images), 3, self.extractor._input_height, self.extractor._input_width), dtype=np.float32)
            for i, img_path in enumerate(images):
                x[i, ...] = self.extractor._general_image_preprocess(utils.load_image(img_path))
            es, rs = self.embedding_fn(x)
            normalize(es, norm='l2', copy=False)

            embeddings[idx:(idx + len(images)), :] = es
            ranks[idx:(idx + len(images))] = rs.flatten()
            idx += len(images)

        embeddings = TSNE(random_state=random_seed).fit_transform(embeddings)
        ranks = scipy.stats.rankdata(ranks).astype(np.int)

        colors = plt.get_cmap('viridis')(np.linspace(0, 1, len(ranks)))
        fig = plt.figure(figsize=(10, 10))
        ax = fig.add_subplot(1, 1, 1)
        for i in range(len(ranks)):
            c = colors[ranks[i] - 1]
            ax.scatter(embeddings[i, 0], embeddings[i, 1], color=c, s=15)
            ax.axis('off')

        return fig

    def conv1_filters(self):
        def vis_square(data):
            """
            Code from: http://nbviewer.jupyter.org/github/BVLC/caffe/blob/master/examples/00-classification.ipynb, with small modifications
            Take an array of shape (n, height, width) or (n, height, width, 3)
            and visualize each (height, width) thing in a grid of size approx. sqrt(n) by sqrt(n)
            """

            # normalize data for display
            data = (data - data.min()) / (data.max() - data.min())

            # force the number of filters to be square
            n = int(np.ceil(np.sqrt(data.shape[0])))
            padding = (((0, n ** 2 - data.shape[0]),
                        (0, 1), (0, 1)) + ((0, 0),) * (data.ndim - 3))  # don't pad the last dimension (if there is one)
            data = np.pad(data, padding, mode='constant',
                          constant_values=1)  # pad with ones (white)

            # tile the filters into an image
            data = data.reshape(
                (n, n) + data.shape[1:]).transpose((0, 2, 1, 3) + tuple(range(4, data.ndim + 1)))
            data = data.reshape(
                (n * data.shape[1], n * data.shape[3]) + data.shape[4:])

            fig = plt.figure(figsize=(10, 10))
            ax = fig.add_subplot(111)
            with plt.rc_context({'image.interpolation': 'nearest', 'image.cmap': 'gray'}):
                ax.imshow(data)
            ax.axis('off')
            return fig

        params = lasagne.layers.get_all_param_values(self.extractor.net[self.extractor.conv1_layer_name])
        filters = params[0].transpose(0, 2, 3, 1)
        fig = vis_square(filters)
        folder_path = os.path.join(
            settings.result_models_root, "conv1filters|%s" % self.NAME)
        boltons.fileutils.mkdir_p(folder_path)
        fig.savefig(os.path.join(folder_path, 'filters-%d.png' % self.log_step))

    def estimates_predictions_corrects_on_test(self):
        test_generator = self.dataset.test_generator(
            batch_size=self.train_batch_size)
        total_estimates = []
        predictions = []
        corrects = []

        for batch in test_generator:
            preprocessed_input = self.extractor.preprocess(batch)
            estimates, target, mask = self._test_rank_estimate(
                preprocessed_input)

            total_estimates.extend(estimates[:sum(mask)])

            estimated_target = self._estimates_to_target_estimates(estimates)

            for p, t, m in zip(estimated_target, target, mask):
                if m:
                    predictions.append(p)
                    if t == 0.5:
                        corrects.append(0.5)
                    elif p == t:
                        corrects.append(1)
                    else:
                        corrects.append(0)

        return np.array(total_estimates).flatten(), predictions, corrects
