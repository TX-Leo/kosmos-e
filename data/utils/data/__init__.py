def get_dataset(dataset_name):
    if dataset_name == 'cornell':
        from .cornell_data import CornellDataset
        return CornellDataset
    elif dataset_name == 'jacquard':
        from .jacquard_data import JacquardDataset
        return JacquardDataset
    elif dataset_name == 'multiObject':
        from .multiObject_data import MultiObjectDataset
        return MultiObjectDataset
    else:
        raise NotImplementedError('Dataset Type {} is Not implemented'.format(dataset_name))
