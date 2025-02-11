
wget https://packages.microsoft.com/config/ubuntu/20.04/packages-microsoft-prod.deb
sudo dpkg -i packages-microsoft-prod.deb
sudo apt-get update
sudo apt-get install blobfuse

CN=unilm
AN=conversationhub
AK=Z9rLBJFxoJNDZWNK0MJ4ZXBFY6aKx11U8cyB19FfFhS/wZ8TlbY3vvQs21Kyq95e7cVtbvy5DZpzthEW2CcbzA==

MOUNT_DIR=/mnt/${AN}
CFG_PATH=~/fuse_connection_${CN}.cfg
MTP=/mnt/localdata/blobfusetmp_temp_${CN}

sudo mkdir -p ${MTP}
sudo chmod 777 ${MTP}

printf 'accountName %s\naccountKey %s\ncontainerName %s\n' ${AN} ${AK} ${CN} > ${CFG_PATH}

sudo chmod 600 ${CFG_PATH}

sudo mkdir -p ${MOUNT_DIR}
sudo chmod 777 ${MOUNT_DIR}
blobfuse ${MOUNT_DIR} --tmp-path=${MTP}  --config-file=${CFG_PATH} -o attr_timeout=240 -o entry_timeout=240 -o negative_timeout=120

