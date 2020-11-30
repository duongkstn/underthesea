# -*- coding: utf-8 -*-
import os
from datetime import datetime
from underthesea.utils import logger, device
from underthesea.data import progress_bar
import torch
import torch.nn as nn
from underthesea.modules.model import BiaffineDependencyModel
from underthesea.utils.sp_config import Config
from underthesea.utils.sp_data import Dataset
from underthesea.utils.sp_field import Field
from underthesea.utils.sp_fn import ispunct
from underthesea.utils.sp_init import PRETRAINED
from underthesea.utils.sp_metric import AttachmentMetric


class DependencyParser(object):
    r"""
    The implementation of Biaffine Dependency Parser.

    References:
        - Timothy Dozat and Christopher D. Manning. 2017.
          `Deep Biaffine Attention for Neural Dependency Parsing`_.

    .. _Deep Biaffine Attention for Neural Dependency Parsing:
        https://openreview.net/forum?id=Hk95PK9le
    """
    NAME = 'biaffine-dependency'
    MODEL = BiaffineDependencyModel

    def __init__(self, embeddings='char', embed=False):
        self.embeddings = embeddings
        self.embed = embed

    def init_model(self, args, model, transform):
        self.args = args
        self.model = model
        self.transform = transform
        try:
            feat = self.args.feat
        except Exception:
            feat = self.args['feat']
        if feat in ('char', 'bert'):
            self.WORD, self.FEAT = self.transform.FORM
        else:
            self.WORD, self.FEAT = self.transform.FORM, self.transform.CPOS
        self.ARC, self.REL = self.transform.HEAD, self.transform.DEPREL
        self.puncts = torch.tensor([i
                                    for s, i in self.WORD.vocab.stoi.items()
                                    if ispunct(s)]).to(device)

    @torch.no_grad()
    def predict(
        self,
        data,
        buckets=8,
        batch_size=5000,
        pred=None,
        prob=False,
        tree=True,
        proj=False,
        verbose=True,
        **kwargs
    ):
        r"""
        Args:
            data (list[list] or str):
                The data for prediction, both a list of instances and filename are allowed.
            pred (str):
                If specified, the predicted results will be saved to the file. Default: ``None``.
            buckets (int):
                The number of buckets that sentences are assigned to. Default: 32.
            batch_size (int):
                The number of tokens in each batch. Default: 5000.
            prob (bool):
                If ``True``, outputs the probabilities. Default: ``False``.
            tree (bool):
                If ``True``, ensures to output well-formed trees. Default: ``False``.
            proj (bool):
                If ``True``, ensures to output projective trees. Default: ``False``.
            verbose (bool):
                If ``True``, increases the output verbosity. Default: ``True``.
            kwargs (dict):
                A dict holding the unconsumed arguments that can be used to update the configurations for prediction.

        Returns:
            A :class:`~underthesea.utils.Dataset` object that stores the predicted results.
        """
        self.transform.eval()
        if prob:
            self.transform.append(Field('probs'))

        logger.info('Loading the data')
        dataset = Dataset(self.transform, data)
        dataset.build(batch_size, buckets)
        logger.info(f'\n{dataset}')

        logger.info('Making predictions on the dataset')
        start = datetime.now()
        loader = dataset.loader
        self.model.eval()

        arcs, rels, probs = [], [], []
        for words, feats in progress_bar(loader):
            mask = words.ne(self.WORD.pad_index)
            # ignore the first token of each sentence
            mask[:, 0] = 0
            lens = mask.sum(1).tolist()
            s_arc, s_rel = self.model(words, feats)
            arc_preds, rel_preds = self.model.decode(s_arc, s_rel, mask,
                                                     tree, proj)
            arcs.extend(arc_preds[mask].split(lens))
            rels.extend(rel_preds[mask].split(lens))
            if prob:
                arc_probs = s_arc.softmax(-1)
                probs.extend([prob[1:i + 1, :i + 1].cpu() for i, prob in zip(lens, arc_probs.unbind())])
        arcs = [seq.tolist() for seq in arcs]
        rels = [self.REL.vocab[seq.tolist()] for seq in rels]
        preds = {'arcs': arcs, 'rels': rels}
        if prob:
            preds['probs'] = probs

        elapsed = datetime.now() - start

        for name, value in preds.items():
            setattr(dataset, name, value)
        if pred is not None:
            logger.info(f'Saving predicted results to {pred}')
            self.transform.save(pred, dataset.sentences)
        logger.info(f'{elapsed}s elapsed, {len(dataset) / elapsed.total_seconds():.2f} Sents/s')

        return dataset

    @torch.no_grad()
    def _evaluate(self, loader):
        self.model.eval()

        total_loss, metric = 0, AttachmentMetric()

        tree = self.args['tree']
        proj = self.args['proj']

        for words, feats, arcs, rels in loader:
            mask = words.ne(self.WORD.pad_index)
            # ignore the first token of each sentence
            mask[:, 0] = 0
            s_arc, s_rel = self.model(words, feats)
            loss = self.model.loss(s_arc, s_rel, arcs, rels, mask)
            arc_preds, rel_preds = self.model.decode(s_arc, s_rel, mask, tree, proj)
            # ignore all punctuation if not specified
            if not self.args['punct']:
                mask &= words.unsqueeze(-1).ne(self.puncts).all(-1)
            total_loss += loss.item()
            metric(arc_preds, rel_preds, arcs, rels, mask)
        total_loss /= len(loader)

        return total_loss, metric

    @classmethod
    def load(cls, path, **kwargs):
        r"""
        Loads a parser with data fields and pretrained model parameters.

        Args:
            path (str):
                - a string with the shortcut name of a pretrained parser defined in ``underthesea.PRETRAINED``
                  to load from cache or download, e.g., ``'crf-dep-en'``.
                - a path to a directory containing a pre-trained parser, e.g., `./<path>/model`.
            kwargs (dict):
                A dict holding the unconsumed arguments that can be used to update the configurations and initiate the model.

        Examples:
            >>> # from underthesea.models.dependency_parser import DependencyParser
            >>> # parser = DependencyParser.load('vi-dp-v1')
            >>> # parser = DependencyParser.load('./tmp/resources/parsers/dp')
        """

        args = Config(**locals())

        if os.path.exists(path):
            state = torch.load(path)
        else:
            path = PRETRAINED[path] if path in PRETRAINED else path
            state = torch.hub.load_state_dict_from_url(path)

        state['args'].update(args)
        args = state['args']

        model = cls().MODEL(**args)
        model.load_pretrained(state['pretrained'])
        model.load_state_dict(state['state_dict'], False)
        model.to(device)
        transform = state['transform']

        parser = cls()
        parser.init_model(args, model, transform)
        return parser

    def save(self, path):
        model = self.model
        if hasattr(model, 'module'):
            model = self.model.module

        args = model.args

        state_dict = {k: v.cpu() for k, v in model.state_dict().items()}
        pretrained = state_dict.pop('pretrained.weight', None)
        state = {'name': self.NAME,
                 'args': args,
                 'state_dict': state_dict,
                 'pretrained': pretrained,
                 'transform': self.transform}
        torch.save(state, path)
