# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/models.base.ipynb (unless otherwise specified).

__all__ = ['TTSModel']

# Cell
from torch import nn


class TTSModel(nn.Module):
    def infer(self):
        raise NotImplemented

    def forward(self):
        raise NotImplemented

    def from_pretrained(self):
        raise NotImplemented

    def to_checkpoint(self):
        return dict(model=self.state_dict())

    @classmethod
    def create(cls, name, opts, folders, all_speakers=True):
        pass


#         model_cls = cls.get_class(name)
#         folders = pd.read_csv(folders)
#         for folder in folders:


#         return model_cls(opts)