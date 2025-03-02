# distplan in ["colossalai", "zero", "ddp"]
export DISTPAN="colossalai"

# The following options only valid when DISTPAN="colossalai"
export TPDEGREE=2
export GPUNUM=4
export PLACEMENT='cpu'
export USE_SHARD_INIT=False

env OMP_NUM_THREADS=16 torchrun --standalone --nproc_per_node=${GPUNUM} train_gpt_demo.py --tp_degree=${TPDEGREE} --placement ${PLACEMENT} --shardinit ${USE_SHARD_INIT} --distplan ${DISTPAN} 2>&1 | tee run.log
