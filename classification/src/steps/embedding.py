import os

import h5py
import numpy as np

from torch.autograd import Variable

from classification.src.loaders.data_managers import SimpleDataManager
from utils import configs, backbones
from utils.io_utils import (
    model_dict,
    path_to_step_output,
    set_and_print_random_seed,
    get_path_to_json,
)


class Embedding():
    """
    This step handles the computing of the embeddings of the evaluation dataset prior to evaluation,
    for computation efficiency. It does not support methods using meta-models, like MAML.
    """

    def __init__(
            self,
            dataset,
            backbone='Conv4',
            method='baseline',
            train_n_way=5,
            test_n_way=5,
            n_shot=5,
            train_aug=False,
            shallow=False,
            split='novel',
            save_iter=-1,
            output_dir=configs.save_dir,
            random_seed=None,
    ):
        """
        Args:
            dataset (str): CUB/miniImageNet
            model (str): Conv{4|6} / ResNet{10|18|34|50|101}
            method (str): baseline/baseline++/protonet/matchingnet/relationnet{_softmax}/maml{_approx}
            train_n_way (int): number of labels in a classification task during training
            test_n_way (int): number of labels in a classification task during testing
            n_shot (int): number of labeled data in each class
            train_aug (bool): perform data augmentation or not during training
            shallow (bool): reduces the dataset to 256 images (typically for quick code testing)
            split (str): which dataset is considered (base, val or novel)
            save_iter (int): save feature from the model trained in x epoch, use the best model if x is -1
        """
        self.dataset = dataset
        self.backbone = backbone
        self.method = method
        self.train_n_way = train_n_way
        self.test_n_way = test_n_way
        self.n_shot = n_shot
        self.train_aug = train_aug
        self.shallow = shallow
        self.split = split
        self.save_iter = save_iter
        self.random_seed = random_seed

        self.checkpoint_dir = path_to_step_output(
            self.dataset,
            self.backbone,
            self.method,
            output_dir=output_dir
        )

    def apply(self, model_state):
        """
        Executes the Embedding step
        Args:
            model_state (dict): contains the whole state of the model that gave the higher validation accuracy

        Returns:
            Tuple[numpy.array, numppy.array]: contain respectively all the features and the corresponding labels
        """
        set_and_print_random_seed(self.random_seed, True, self.checkpoint_dir)

        if self.method in ['maml', 'maml_approx']:
            print("MAML doesn't support the step Embedding. Going on to the next step ...")
            return None
        # Load trained parameters into backbone
        model = self._load_model(model_state)

        data_loader, outfile = self._get_data_loader_and_outfile()

        return self._save_features(model, data_loader, outfile)

    def dump_output(self, _, output_folder, output_name, **__):
        pass

    def _save_features(self, model, data_loader, outfile):
        """
        Computes and save the embeddings of all images with the given feature extractor
        Args:
            model (nn.Module): trained feature extractor
            data_loader (torch.Dataloader): contains all examples of novel dataset, in batches
            outfile (str): where to save features

        Returns:
            Tuple[numpy.array, numppy.array]: contain respectively all the features and the corresponding labels
        """
        f = h5py.File(outfile, 'w')
        max_count = len(data_loader) * data_loader.batch_size
        print(data_loader.batch_size, max_count)
        all_labels = f.create_dataset('all_labels', (max_count,), dtype='i')
        all_feats = None
        all_labels_array=np.zeros((max_count,), dtype=int)
        all_feats_array=None
        count = 0
        # TODO: here, last batch is smaller than batch_size, thus the last columns of all_feats are empty (and deleted in feature_loader.py)
        for i, (x, y) in enumerate(data_loader):
            if i % 100 == 0:
                print('{:d}/{:d}'.format(i, len(data_loader)))

            x = x.cuda()
            x_var = Variable(x)
            feats = model(x_var)
            if all_feats is None:
                all_feats = f.create_dataset('all_feats', [max_count] + list(feats.size()[1:]), dtype='f')
                all_feats_array=np.zeros([max_count] + list(feats.size()[1:]), dtype=np.float32)
            all_feats[count:count + feats.size(0)] = feats.data.cpu().numpy()
            all_labels[count:count + feats.size(0)] = y.cpu().numpy()
            all_feats_array[count:count + feats.size(0)] = feats.data.cpu().numpy()
            all_labels_array[count:count + feats.size(0)] = y.cpu().numpy()
            count = count + feats.size(0)

        count_var = f.create_dataset('count', (1,), dtype='i')
        count_var[0] = count
        f.close()
        return (all_feats_array, all_labels_array)

    def _get_data_loader_and_outfile(self):
        """
        Returns data loaders and path to outfile
        Returns:
            tuple : data_loader and outfile
        """
        # TODO: unify with train.py
        assert self.method != 'maml' and self.method != 'maml_approx', 'maml do not support save_feature and run'

        # Defines image size
        if 'Conv' in self.backbone:
            image_size = 84
        else:
            image_size = 224

        path_to_data_file = get_path_to_json(self.dataset, self.split)

        # Defines output file for computed features
        #TODO no need for outfile anymore
        if self.save_iter != -1:
            outfile = os.path.join(self.checkpoint_dir,
                                   f'{self.split}_{self.save_iter}.hdf5')
        else:
            outfile = os.path.join(self.checkpoint_dir, self.split + ".hdf5")

        # Return data loader TODO: why do we do batches here ?
        datamgr = SimpleDataManager(image_size, batch_size=64)
        data_loader = datamgr.get_data_loader(path_to_data_file, aug=False, shallow=self.shallow)

        dirname = os.path.dirname(outfile)
        if not os.path.isdir(dirname):
            os.makedirs(dirname)

        return (data_loader, outfile)

    def _load_model(self, model_state):
        """
        Loads model from training and returns its feature layers
        Args:
            model_state (dict): contains the state of the trained model. If None, loads from .tar file

        Returns:
            nn.Module: trained features extractor
        """
        state = model_state['state'].copy()

        state_keys = list(state.keys())

        # Create backbone
        if self.method in ['relationnet', 'relationnet_softmax']:
            if self.backbone == 'Conv4':
                model = backbones.Conv4NP()
            elif self.backbone == 'Conv6':
                model = backbones.Conv6NP()
            elif self.backbone == 'Conv4S':
                model = backbones.Conv4SNP()
            else:
                model = model_dict[self.backbone](flatten=False)
        else:
            model = model_dict[self.backbone]()

        model = model.cuda()

        # Keep only feature layers
        for state_key in state_keys:
            if "feature." in state_key:
                newkey = state_key.replace("feature.", "")
                # an architecture model has attribute 'feature', load architecture feature to backbone by casting name from 'feature.trunk.xx' to 'trunk.xx'
                state[newkey] = state.pop(state_key)
            else:
                state.pop(state_key)

        model.load_state_dict(state)
        model.eval()

        return model
