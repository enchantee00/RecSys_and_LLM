import argparse
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pickle
import random
import time
from argparse import Namespace
from logging import getLogger

import numpy as np
import torch
import torch.multiprocessing as mp
from huggingface_hub import hf_hub_download, snapshot_download
from recbole.data import create_dataset, data_preparation
from recbole.utils import init_logger, init_seed
from torch.distributed import destroy_process_group, init_process_group
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm

from ML.models.ALLMRec.a_llmrec_model import *
from ML.models.ALLMRec.pre_train.sasrec.utils import *
from ML.models.gSASRec.gsasrec_inference import *
from ML.models.LGCN.model import MLGCN
from ML.models.TiSASRec.TiSASRec_inference import *
from ML.utils import get_missing


def setup_ddp(rank, world_size):
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = "12355"
    init_process_group(backend="nccl", rank=rank, world_size=world_size)
    torch.cuda.set_device(rank)


def load_data_and_model(model_file, data_path):
    checkpoint = torch.load(model_file)
    config = checkpoint["config"]
    init_seed(config["seed"], config["reproducibility"])
    config["data_path"] = data_path

    dataset = create_dataset(config)
    train_data, valid_data, test_data = data_preparation(config, dataset)
    config["model"] = "MLGCN" if config["model"] == "LightGCN" else "MODEL NAME ERROR"
    print(f"######### LOAD MODEL : {config["model"]} #########")
    if config["model"] == "MLGCN":
        model = MLGCN
    else:
        raise ValueError("`model_name` [{}] is not the name of an existing model.")
    init_seed(config["seed"], config["reproducibility"])
    model = model(config, train_data._dataset).to(config["device"])
    model.load_state_dict(checkpoint["state_dict"])
    model.load_other_parameter(checkpoint.get("other_parameter"))

    return config, model, dataset, train_data, valid_data, test_data


def prepare(model_version: str = "LightGCN-Jan-08-2025_10-28-58"):
    data_path = snapshot_download(
        repo_id="PNUDI/recbole",
        cache_dir="../ML/data",
        repo_type="dataset",
    )
    model_path = hf_hub_download(
        repo_id="PNUDI/LightGCN",
        filename=f"{model_version}.pth",
        cache_dir="../ML/models/saved_models",
        repo_type="model",
    )
    with open(f"{data_path}/Movies_and_TV_text_name_dict.json.gz", "rb") as f:
        expected_dict = pickle.load(f)

    # model, dataset 불러오기
    _, model, dataset, _, _, _ = load_data_and_model(model_path, data_path)

    return expected_dict["title"], model, dataset


def predict(
    user_token: str, token2title: dict, missing_list, model, dataset, topk: int = 20
):
    matrix = dataset.inter_matrix(form="csr")
    model.eval()
    user_id = dataset.token2id("user_id", user_token)
    score = model.full_sort_predict(user_id)
    rating_pred = score.cpu().data.numpy().copy()

    interacted_indices = matrix[user_id].indices
    rating_pred[interacted_indices] = 0
    ind = np.argpartition(rating_pred, -topk)[-topk:]

    arr_ind = rating_pred[ind]
    arr_ind_argsort = np.argsort(arr_ind)[::-1]
    batch_pred_list = ind[arr_ind_argsort]

    # print(f'user_token : {user_token}, user_id : {user_id}')
    # print(f'item_pred_ids : {batch_pred_list}, item_pred_tokens : {[dataset.token2id('item_id', item) for item in batch_pred_list]}')

    return [
        token2title[int(dataset.id2token("item_id", ele).item())]
        for ele in batch_pred_list
        if int(dataset.id2token("item_id", ele).item()) not in missing_list
    ][: topk // 2]


class ModelManager:
    def __init__(self):
        """모델 및 데이터 로드"""
        self.allmrec_model = None
        self.lgcn_model = None
        self.tisasrec_model = None
        self.gsasrec_model = None
        self.expected_dict = None
        self.lgcn_dataset = None
        self.tisasrec_dataset = None
        self.gsasrec_dataset = None
        self.missing_list = None
        self.llmrec_args = Namespace(
            multi_gpu=False,  # Multi-GPU 사용 여부
            gpu_num=0,  # GPU 번호
            llm="opt",  # LLM 모델 선택
            recsys="sasrec",  # RecSys 모델 선택
            rec_pre_trained_data="Movies_and_TV",  # 데이터셋 설정
            pretrain_stage1=False,  # Pretrain 단계 1 활성화
            pretrain_stage2=False,  # Pretrain 단계 2 비활성화
            inference=True,  # Inference 비활성화
            batch_size1=32,  # 단계 1 배치 크기
            batch_size2=2,  # 단계 2 배치 크기
            batch_size_infer=2,  # 추론 배치 크기
            maxlen=50,  # 최대 시퀀스 길이
            num_epochs=10,  # 에포크 수
            stage1_lr=0.0001,  # 단계 1 학습률
            stage2_lr=0.0001,  # 단계 2 학습률
            device="cuda:0",  # 디바이스 설정
        )
        self.tisasrec_args = Namespace(
            dataset="Movies_and_TV_Time",
            batch_size=200,
            lr=0.001,
            maxlen=50,
            hidden_units=64,
            num_blocks=2,
            num_epochs=1000,
            num_heads=1,
            dropout_rate=0.2,
            l2_emb=0.00005,
            device="cuda:0",
            time_span=256,
            state_dict_path="ML/models/saved_models/TiSASRec_model_epoch_1000.pt",
        )
        self.gsasrec_args = Namespace(
            dataset_name="Movies_and_TV",
            sequence_length=100,
            embedding_dim=64,
            num_heads=1,
            max_batches_per_epoch=500,
            num_blocks=2,
            dropout_rate=0.5,
            negs_per_pos=256,
            gbce_t=0.75,
            state_dict_path="ML/models/saved_models/gsasrec-Movies_and_TV-step=326197.pt",
            device="cuda:0",
        )
        # self._load_models()

    """
    def test_data(self):
        with open(f'ML/data/Movies_and_TV_text_name_dict.json.gz','rb') as ft:
            text_name_dict = pickle.load(ft)
        self.tisasrec_dataset = data_partition(self.tisasrec_args.dataset)
        User, usernum, itemnum, timenum, item_map, reverse_item_map = self.tisasrec_dataset
        #print(itemnum)
        self.gsasrec_model = build_model(self.gsasrec_args, itemnum)
        self.gsasrec_model.to(self.gsasrec_args.device)
        self.gsasrec_model.load_state_dict(torch.load(self.gsasrec_args.state_dict_path))
        self.gsasrec_model.eval()
        a, b = gsasrec_recommend_top5(self.gsasrec_model, self.tisasrec_dataset, 978, self.gsasrec_args, text_name_dict)
        print(a)
        print(b)
        """

    def _load_models(self):
        """모델 및 데이터 로드 로직"""
        # Load ALLMRec Model

        self.allmrec_model = A_llmrec_model(self.llmrec_args).to(
            self.llmrec_args.device
        )
        phase1_epoch = 10
        phase2_epoch = 10
        self.allmrec_model.load_model(
            self.llmrec_args, phase1_epoch=phase1_epoch, phase2_epoch=phase2_epoch
        )
        self.allmrec_model.eval()

        # Load LGCN Model and Dataset
        self.expected_dict, self.lgcn_model, self.lgcn_dataset = prepare()
        self.missing_list = get_missing(self.expected_dict)

        # Load TiSASRec and Dataset
        self.tisasrec_dataset = data_partition(self.tisasrec_args.dataset)
        User, usernum, itemnum, timenum, item_map, reverse_item_map = (
            self.tisasrec_dataset
        )
        self.tisasrec_model = tisasrec_initialize_model(
            self.tisasrec_args, usernum, itemnum
        )
        self.tisasrec_model.load_state_dict(
            torch.load(self.tisasrec_args.state_dict_path)
        )
        self.tisasrec_model.eval()
        self.gsasrec_model = build_model(self.gsasrec_args, itemnum)
        self.gsasrec_model.to(self.gsasrec_args.device)
        self.gsasrec_model.load_state_dict(
            torch.load(self.gsasrec_args.state_dict_path)
        )
        self.gsasrec_model.eval()
        # Load gSASRec and Dataset
        self.gsasrec_model = build_model(self.gsasrec_args, itemnum)
        self.gsasrec_model.to(self.gsasrec_args.device)
        self.gsasrec_model.load_state_dict(
            torch.load(self.gsasrec_args.state_dict_path)
        )
        self.gsasrec_model.eval()

    def inference(self, user_id):
        """ALLMRec 및 LGCN 모델 기반 추론"""
        # LGCN 모델 기반 예측
        lgcn_predictions = "no"
        lgcn_predictions = predict(
            str(user_id),
            self.expected_dict,
            self.missing_list,
            self.lgcn_model,
            self.lgcn_dataset,
        )
        lgcn_predictions = "\n".join(lgcn_predictions)

        # ALLMRec 모델 기반 예측
        dataset = load_data(
            self.allmrec_model.args.rec_pre_trained_data,
            self.llmrec_args.maxlen,
            path=f"./ML/data/amazon/{self.allmrec_model.args.rec_pre_trained_data}.txt",
        )
        [data, usernum, itemnum] = dataset

        if user_id <= 0 or user_id > usernum:
            return {"error": "Invalid user_id"}

        seq = np.expand_dims(np.array(data[user_id]), axis=0)
        allmrec_prediction = self.allmrec_model([user_id, seq], mode="inference")

        print(allmrec_prediction)

        # TiSASRec
        tisasrec_tok5_title, tisasrec_tok5_num = tisasrec_recommend_top5(
            self.tisasrec_args,
            self.tisasrec_model,
            user_id,
            self.tisasrec_dataset,
            self.expected_dict,
        )

        # gSASRec
        gsasrec_tok5_title, gsasrec_tok5_num = gsasrec_recommend_top5(
            self.gsasrec_model,
            self.tisasrec_dataset,
            978,
            self.gsasrec_args,
            text_name_dict,
        )

        return {
            "lgcn_predictions": lgcn_predictions,
            "allmrec_prediction": allmrec_prediction,
        }
