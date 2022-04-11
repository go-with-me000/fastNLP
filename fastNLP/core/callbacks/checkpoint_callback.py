__all__ = [
    'ModelCheckpointCallback',
    'TrainerCheckpointCallback'
]
import os
from typing import Union, Optional, Callable, Dict, Sequence, Any, Mapping
from pathlib import Path
from abc import ABC
import sys


import fastNLP
from .callback import Callback, Filter
from fastNLP.core.callbacks.utils import _get_monitor_value
from fastNLP.core.log import logger
from fastNLP.envs import FASTNLP_LAUNCH_TIME
from fastNLP.core.utils import synchronize_safe_rm, synchronize_mkdir
from fastNLP.core.utils import apply_to_collection


class CanItemDataType(ABC):
    """
    检测可以进行传输的对象。

    """

    @classmethod
    def __subclasshook__(cls, subclass: Any) -> Union[bool, Any]:
        if cls is CanItemDataType:
            item = getattr(subclass, 'item', None)
            return callable(item)
        return NotImplemented



class CheckpointCallback(Callback):
    def __init__(
            self,
            monitor,
            save_folder: Optional[Union[str, Path]] = None,
            save_every_n_epochs: Optional[int] = None,
            save_every_n_batches: Optional[int] = None,
            save_last: bool = True,
            save_topk: Optional[int] = None,
            save_on_exception: Optional[Union[BaseException, Sequence[BaseException]]] = None,
            larger_better: bool = True,
            only_state_dict: bool = True,
            model_save_fn: Optional[Callable] = None,
            **kwargs,
    ):
        if monitor is None and save_topk is not None:
            raise ValueError("Parameter `monitor` must be set when you want to use 'save_topk'.")

        if monitor is not None and not isinstance(monitor, str):
            raise ValueError("Parameter `monitor` should be of 'str' type.")

        if save_folder is None:
            logger.warning(
                "Parameter `path` is None, and we will use the current work directory to find and load your model.")
            save_folder = Path.cwd()
        if not save_folder.exists():
            raise NotADirectoryError(f"Path '{save_folder.absolute()}' is not existed!")
        elif save_folder.is_file():
            raise ValueError("Parameter `save_folder` should be a directory instead of a file.")

        if save_every_n_epochs is not None:
            if not isinstance(save_every_n_epochs, int) or save_every_n_epochs < 1:
                raise ValueError("parameter save_after_epoch_num should be an int and greater than or equal to 1.")

        else:
            save_every_n_epochs = sys.maxsize  # 使得没有数字可以整除

        if save_every_n_batches is not None:
            if not isinstance(save_every_n_batches, int) or save_every_n_batches < 1:
                raise ValueError(
                    "parameter save_every_n_batches should be an int and greater than or equal to 1.")
        else:
            save_every_n_batches = sys.maxsize  # 使得没有数字可以整除

        if save_topk is not None:
            if not isinstance(save_topk, int) or save_topk < 1:
                raise ValueError("parameter save_topk should be an int and greater than or equal to 1.")

        if save_on_exception is not None:
            if not isinstance(save_on_exception, Sequence):
                save_on_exception = [save_on_exception]

            for exception in save_on_exception:
                if not issubclass(exception, BaseException):
                    raise TypeError("Each exception in parameter `save_on_exception` can only be "
                                    "`BaseException` type.")
        else:
            save_on_exception = []
        self.monitor = monitor
        self.save_folder = Path(save_folder)
        self.save_every_n_epochs = save_every_n_epochs
        self.save_every_n_batches = save_every_n_batches
        self.save_last = save_last
        self.save_topk = save_topk
        self.larger_better = larger_better
        self.only_state_dict = only_state_dict
        self.model_save_fn = model_save_fn
        self.save_on_exception = save_on_exception
        self.kwargs = kwargs

        # 这些参数是专门留给 topk 模式专门使用的；
        self._topk_model = {}
        self._topn = 0  # 表示目前已经保存了几个最好的模型；

        # 因为我们在 `_get_validate_metric` 函数中，当在返回的 `validate_res` 字典中找不到 `monitor` 时，是使用匹配找到的
        #  key 对应的 value 当做结果；但是这样存在的一个问题在于如果用户传入的 metric 返回的 sub_metric 的名字可能会混淆，并且其在下一次
        #  训练的代码中修改了这些 sub_metric 返回的顺序，那么就会导致模糊匹配拿到的 key 和 value 与之前的不是同一个，这显然不是合理的行为；
        # 因此我们通过该变量来表示我们通过模糊匹配拿到的 key；
        self._real_monitor = self.monitor

        # 注意这里应当保证只有进程 0 在执行这个操作，因为当用户使用 python -m torch.distributed.launch 来拉起进程的时候，
        #  FASTNLP_LAUNCH_TIME 在每一个进程上的值是不一样的；
        self.timestamp_path = self.save_folder.joinpath(os.environ[FASTNLP_LAUNCH_TIME])
        # 我们只需要保证这个创建文件夹的操作只在进程 0 上进行即可；因为后续的实际的保存操作，其它进程实际并不会去执行；
        synchronize_mkdir(self.timestamp_path)

    def on_validate_end(self, trainer, validate_res):
        self._save_topk(trainer, validate_res)

    def on_train_epoch_end(self, trainer: "fastNLP.Trainer"):
        if trainer.cur_epoch_idx % self.save_every_n_epochs == 0:
            folder_name = f'{self.folder_prefix}-epoch_{trainer.cur_epoch_idx}'
            self.save(trainer, folder_name=folder_name)
        if self.save_last:
            folder_name = f'{self.folder_prefix}-last'
            self.save(trainer, folder_name=folder_name)

    def on_train_batch_end(self, trainer):
        if trainer.global_forward_batches % self.save_every_n_batches == 0:
            folder_name = f'{self.folder_prefix}-epoch_{trainer.cur_epoch_idx}-batch_{trainer.global_forward_batches}'
            self.save(trainer, folder_name=folder_name)

    def on_exception(self, trainer, exception: BaseException):
        if exception.__class__ in self.save_on_exception:
            folder_name = f'{self.folder_prefix}-epoch_{trainer.cur_epoch_idx}-batch_{trainer.global_forward_batches}-' \
                     f'exception_{exception.__class__.__name__}'
            self.save(trainer=trainer, folder_name=folder_name)

    def on_sanity_check_end(self, trainer, sanity_check_res):
        # 主要核对一下 monitor 是否存在。
        self._get_validate_metric(sanity_check_res)

    def on_save_checkpoint(self, trainer) -> Dict:
        """
        保存 timestamp_path 使得之后可以继续训练并保存到该文件夹。
        topk_model的状态
        _real_monitor的值
        """

        states = {}
        states['timestamp_path'] = str(self.timestamp_path.absolute())
        states['_topk_model'] = apply_to_collection(self._topk_model, dtype=CanItemDataType,
                                                    function=lambda x:x.item())
        states['save_topk'] = 0 if self.save_topk is None else self.save_topk
        states['_real_monitor'] = self._real_monitor
        return states

    def on_load_checkpoint(self, trainer, states: Optional[Dict]):
        timestamp_path = states['timestamp_path']
        if not os.path.exists(timestamp_path):
            logger.info(f"The resuming save folder {timestamp_path} is not exists, will checkpoint save to "
                        f" {self.timestamp_path.absolute()}.")
        else:
            logger.info(f"Resume to save in path: {timestamp_path}.")
            self.timestamp_path = Path(timestamp_path)
            _topk_model = states['_topk_model']
            save_topk = None if int(states['save_topk']) == 0 else int(states['save_topk'])
            if save_topk is not None and self.save_topk is not None:
                assert self.save_topk == save_topk, f"The checkpoint set save_topk={save_topk}, while this callback set it " \
                                                    f"as {save_topk}."
            self._topk_model.update(self._topk_model)
        self._real_monitor = states["real_monitor"]

    def _save_topk(self, trainer: "fastNLP.Trainer", validate_res: Dict):
        """
        根据validate_res决定保存哪些model的函数。会自动移除掉不满足topk的文件夹。

        :param trainer:
        :param validate_res:
        :return:
        """
        if self.save_topk is not None:
            _metric_value = self._get_validate_metric(validate_res)
            folder_name = f"{self.folder_prefix}-epoch_{trainer.cur_epoch_idx}-batch_{trainer.global_forward_batches}" \
                         f"-{self._real_monitor}_{_metric_value}"

            _should_save = False
            if self._topn < self.save_topk:
                self._topk_model[folder_name] = _metric_value
                self._topn += 1
                _should_save = True
            else:
                _least_valuable_model = (min if self.larger_better else max)(self._topk_model,
                                                                             key=lambda x: self._topk_model[x])
                if (self.larger_better and _metric_value > self._topk_model[_least_valuable_model]) or \
                        (self.larger_better is False and _metric_value < self._topk_model[_least_valuable_model]):
                    self._topk_model[folder_name] = _metric_value
                    _should_save = True
                    self._topk_model.pop(_least_valuable_model)
                    synchronize_safe_rm(self.timestamp_path.joinpath(_least_valuable_model))

                assert len(self._topk_model) == self.save_topk == self._topn

            if _should_save:
                self.save(trainer, folder_name=folder_name)

    def save(self, trainer, folder_name):
        """
        执行保存的函数，将数据保存在 save_folder/timestamp/folder_name 下。

        :param trainer:
        :param folder_name:
        :return:
        """
        folder = self.timestamp_path.joinpath(folder_name)
        synchronize_mkdir(folder)
        _fn = getattr(trainer, self.save_fn_name)
        _fn(
            folder=folder,
            only_state_dict=self.only_state_dict,
            model_save_fn=self.model_save_fn,
            **self.kwargs
        )

    def _get_validate_metric(self, res: Dict):
        """
        该函数用于从 `Evaluator` 的结果中找到属于当前 CheckpointCallback 的 metric result（根据 monitor）；
        如果用户输入在 res 中没有找到，我们会查询所有的 validate 结果字典的键值，根据 最长公共字符串 匹配，使用最长匹配的结果值；
        :param res:
        :return:
        """
        use_monitor, value = _get_monitor_value(monitor=self.monitor, real_monitor=self._real_monitor, res=res)
        self._real_monitor = use_monitor
        return value

    @property
    def folder_prefix(self):
        raise NotImplementedError("The `folder_prefix` is not specified")

    @property
    def save_fn_name(self):
        raise NotImplementedError("The `save_fn_name` is not specified.")


class ModelCheckpointCallback(CheckpointCallback):
    """
    保存模型 checkpoint 的  callback ，其保存的文件目录以及文件名命名规则如下

    - save_folder/
        - YYYY-mm-dd-HH_MM_SS_fffff/  # 自动根据当前脚本的启动时间创建的
            - model-epoch_{epoch_idx}/  # 满足 save_every_n_epochs 条件保存的模型
            - model-epoch_{epoch_idx}-batch_{global_batch_idx}/  # 满足 save_every_n_batches 保存的模型
            - model-last/  # 最后一个 epoch 的保存
            - model-epoch_{epoch_idx}-batch_{global_batch_idx}-exception_{exception_type}/  # exception时保存。
            - model-epoch_{epoch_idx}-batch_{global_batch_idx}-{monitor}_{monitor_value}/  # 满足topk条件存储文件名

    model_save_fn 为 None ，则以上每个 folder 中，将生成 fastnlp_model.pkl.tar 文件。
    若 model_save_fn 不为 None，则 fastNLP 将 folder 绝对路径传递给该函数，fastNLP 不在该 folder 下创建任何文件。

    :param monitor: 监控的 metric 的名称。如果在 evaluation 结果中没有找到完全一致的名称，将使用 最短公共字符串算法 找到最匹配
        的那个作为 monitor 。
    :param save_folder: 保存的文件夹，fastNLP 将在该文件下以时间戳创建子文件夹，并在里面保存。因此不同次运行可以将被保存到不同的
        时间戳文件夹中。如果为 None ，默认使用当前文件夹。
    :param save_every_n_epochs: 多少个 epoch 保存一次。
    :param save_every_n_batches: 多少个 batch 保存一次。
    :param save_last: 如果为 True ，将在每次 epoch 运行结束都保存一次，会覆盖之前的保存。
    :param save_topk: 保存 monitor 结果 topK 个。
    :param save_on_exception: 在出异常信息时，是否保存。传入需要捕获的异常的类。
    :param larger_better: monitor 的值是否时越大越好。
    :param only_state_dict: 保存模型时是否只保存 state_dict 。当 model_save_fn 不为 None 时，该参数无效。
    :param model_save_fn: 个性化的保存函数，当触发保存操作时，就调用这个函数，这个函数应当接受一个文件夹作为参数，不返回任何东西。
        如果传入了 model_save_fn 函数，fastNLP 将不再进行模型相关的保存。在多卡场景下，我们只在 rank 0 上会运行该函数。
    :param kwargs:
    """
    @property
    def save_fn_name(self):
        return 'save_model'

    @property
    def callback_name(self):
        """
        通过该值决定两个 CheckpointCallback 实例是否可以共用断点重训的状态；
        :return:
        """
        return f"model_checkpoint#monitor-{self.monitor}#topK-{self.save_topk}#only_state_dict-{self.only_state_dict}"

    @property
    def folder_prefix(self):
        return 'model'


class TrainerCheckpointCallback(CheckpointCallback):
    """
    保存 Trainer checkpoint 的  callback ，其保存的文件目录以及文件名命名规则如下

    - save_folder/
        - YYYY-mm-dd-HH_MM_SS_fffff/  # 自动根据当前脚本的启动时间创建的
            - trainer-epoch_{epoch_idx}/  # 满足 save_every_n_epochs 条件保存的模型
            - trainer-epoch_{epoch_idx}-batch_{global_batch_idx}/  # 满足 save_every_n_batches 保存的模型
            - trainer-last/  # 最后一个 epoch 的保存
            - trainer-epoch_{epoch_idx}-batch_{global_batch_idx}-exception_{exception_type}/  # exception时保存。
            - trainer-epoch_{epoch_idx}-batch_{global_batch_idx}-{monitor}_{monitor_value}/  # 满足topk条件存储文件名

    model_save_fn 为 None ，则以上每个 folder 中，将生成两个文件：fastnlp_trainer.pkl.tar 以及 fastnlp_model.pkl.tar 。
    若 model_save_fn 不为 None，则 fastNLP 只会在每个 folder 下生成 fastnlp_trainer.pkl.tar 文件。

    :param monitor: 监控的 metric 的名称。如果在 evaluation 结果中没有找到完全一致的名称，将使用 最短公共字符串算法 找到最匹配
        的那个作为 monitor 。
    :param save_folder: 保存的文件夹，fastNLP 将在该文件下以时间戳创建子文件夹，并在里面保存。因此不同次运行可以将被保存到不同的
        时间戳文件夹中。如果为 None ，默认使用当前文件夹。
    :param save_every_n_epochs: 多少个 epoch 保存一次。
    :param save_every_n_batches: 多少个 batch 保存一次。
    :param save_last: 如果为 True ，将在每次 epoch 运行结束都保存一次，会覆盖之前的保存。
    :param save_topk: 保存 monitor 结果 topK 个。
    :param save_on_exception: 在出异常信息时，是否保存。
    :param larger_better: monitor 的值是否时越大越好。
    :param only_state_dict: 保存模型时是否只保存 state_dict 。当 model_save_fn 不为 None 时，该参数无意义。
    :param model_save_fn: 个性化的保存函数，当触发保存操作时，就调用这个函数，这个函数应当接受一个文件夹作为参数，不返回任何东西。
        如果传入了 model_save_fn 函数，fastNLP 将不再进行模型相关的保存。在多卡场景下，我们只在 rank 0 上会运行该函数。
    :param kwargs:
    """
    @property
    def save_fn_name(self):
        return 'save'

    @property
    def callback_name(self):
        """
        通过该值决定两个 CheckpointCallback 实例是否可以共用断点重训的状态；
        :return:
        """
        return f"trainer_checkpoint#monitor-{self.monitor}#topK-{self.save_topk}#only_state_dict-{self.only_state_dict}"

    @property
    def folder_prefix(self):
        return 'trainer'