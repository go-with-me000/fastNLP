import pytest

from fastNLP.core.controllers.trainer import Trainer
from fastNLP.core.callbacks import Events
from tests.helpers.utils import magic_argv_env_context


@magic_argv_env_context
def test_trainer_torch_without_evaluator():
    @Trainer.on(Events.ON_TRAIN_EPOCH_BEGIN(every=10))
    def fn1(trainer):
        pass

    @Trainer.on(Events.ON_TRAIN_BATCH_BEGIN(every=10))
    def fn2(trainer, batch, indices):
        pass

    with pytest.raises(AssertionError):
        @Trainer.on(Events.ON_TRAIN_BATCH_BEGIN(every=10))
        def fn3(trainer, batch):
            pass



