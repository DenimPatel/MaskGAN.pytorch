import torch
from torch import nn
from collections import namedtuple
from mgan.utils.perplexity import perplexity, greedy_sample

from mgan.criterions import \
        TCELoss,            \
        REINFORCE,          \
        TBCELoss,           \
        WeightedMSELoss

from mgan.models import     \
        MLEGenerator,       \
        MGANDiscriminator,  \
        MGANGenerator,      \
        MGANCritic

class LossModel(nn.Module):
    def __init__(self, model, criterion):
        super().__init__()
        self.model = model
        self.criterion = criterion


class MGANModel(nn.Module):
    def __init__(self, generator, discriminator, critic=None, pretrain=False):
        super().__init__()
        self.generator = generator
        self.discriminator = discriminator
        self.critic = critic
        self.pretrain = pretrain

    @classmethod
    def build_model(cls, args, task, pretrain):
        # Build critic
        critic = MGANCritic.build_model(args, task)
        mse_loss = WeightedMSELoss()
        closs = LossModel(critic, mse_loss)

        # Build generator
        if pretrain:
            generator = MLEGenerator.build_model(args,task)
            gcriterion = TCELoss()
        else:
            generator = MGANGenerator.build_model(args, task)
            reinforce = REINFORCE(gamma=0.01, clip_value=5.0)
            gcriterion = reinforce

        gloss = LossModel(generator, gcriterion)

        # Build discriminator
        discriminator = MGANDiscriminator.build_model(args, task)
        tceloss = TBCELoss()
        dloss = LossModel(discriminator, tceloss)

        return cls(gloss, dloss, closs, pretrain=pretrain)

    def forward(self, *args, **kwargs):
        if 'ppl' not in kwargs:
            kwargs['ppl'] = False

        if kwargs['tag'] == 'g-step':
            if self.pretrain:
                return self._gstep_pretrain(*args, ppl_compute=kwargs['ppl'])
            else:
                return self._gstep(*args, ppl_compute=kwargs['ppl'])
        elif kwargs['tag'] == 'c-step':
            return self._cstep(*args)

        return self._dstep(*args, real=kwargs['real'])

    def _cstep(self, masked, lengths, mask, unmasked):
        with torch.no_grad():
            samples, log_probs, attns = self.generator.model(masked, lengths, unmasked, mask)
            logits, attn_scores = self.discriminator.model(masked, lengths, samples)

        baselines, _ = self.critic.model(masked, lengths, samples)
        with torch.no_grad():
            reward, cumulative_rewards = self.generator.criterion(log_probs, logits, mask, baselines)

        critic_loss = self.critic.criterion(baselines.squeeze(2), cumulative_rewards, mask)
        return critic_loss

    def _gstep(self, masked, lengths, mask, unmasked, ppl_compute=False):
        samples, log_probs, attns = self.generator.model(masked, lengths, unmasked, mask)
        
        # discriminattor
        with torch.no_grad():
            logits, attn_scores = self.discriminator.model(masked, lengths, samples)
            baselines, _ = self.critic.model(masked, lengths, samples)

        reward, cumulative_rewards = self.generator.criterion(log_probs, logits, mask, baselines.detach())
        loss = -1*reward

        # Compute perplexity
        if ppl_compute:
            with torch.no_grad():
                logits = self.generator.model.logits(masked, lengths, unmasked, mask).clone()
                log_probs = torch.nn.functional.log_softmax(logits, dim=2)
                ppl = perplexity(unmasked, samples, log_probs)
        else:
            ppl = None

        return (loss, samples, ppl)
    

    def _gstep_pretrain(self, masked, lengths, mask, unmasked, ppl_compute=False):
        logits, attns = self.generator.model(masked, lengths, unmasked)
        samples = greedy_sample(logits)
        loss = self.generator.criterion(logits, unmasked)
        if ppl_compute:
            with torch.no_grad():
                log_probs = torch.nn.functional.log_softmax(logits, dim=2).clone()
                ppl = perplexity(unmasked, samples, log_probs)
        else:
            ppl = None
        return (loss, samples, ppl)

    def _dstep(self, masked, lengths, mask, unmasked, real=True):
        logits, attn_scores = self.discriminator.model(masked, lengths, unmasked)
        mask = mask.unsqueeze(2)
        truths = torch.ones_like(logits) if real else torch.ones_like(logits) - mask
        loss = self.discriminator.criterion(logits, truths, weight=mask)
        return loss
