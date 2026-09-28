import hashlib


def calculate_file_checksum(file_path, logger, algorithm='sha256') -> str | None:
    """
    Calculate the checksum of a file using the specified algorithm.

    Args:
        file_path (str): Path to the file.
        algorithm (str): Hash algorithm to use (e.g., 'md5', 'sha1', 'sha256'). Default is 'sha256'.

    Returns:
        str: The checksum of the file content.
        :param algorithm:
        :param file_path:
        :param logger:
    """
    hash_func = hashlib.new(algorithm)
    logger.debug(f'Calculating checksum for {file_path} using [{algorithm}] algorithm.')
    checksum = None
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b''):
                hash_func.update(chunk)
        checksum = hash_func.hexdigest()
    except FileNotFoundError:
        logger.error(f"File not found: {file_path}")
    except Exception as e:
        logger.error(f"An error occurred: {e}", exc_info=True)
    return checksum
