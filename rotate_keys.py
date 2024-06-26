import boto3
import requests
import sys
import os
from base64 import b64encode
from nacl import encoding, public

# checks if values set to override default or set default
access_key_name = os.environ.get('GITHUB_ACCESS_KEY_NAME', 'access_key_id')
secret_key_name = os.environ.get('GITHUB_SECRET_KEY_NAME', 'secret_key_id')

# sets creds for boto3
iam = boto3.client(
    'iam',
    aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
    aws_session_token=os.environ.get('AWS_SESSION_TOKEN')
)


def main_function():
    '''The main function that runs the script. It will:
    - Get the current IAM user
    - Get the current number of access keys
    - Generate new access keys
    - Get the public key from the GitHub repository
    - Encrypt the new access keys
    - Upload the new access keys to the GitHub repository
    - Delete the old access keys

    If the number of access keys is already 2, the script will exit with a status code of 1.
    If the script fails at any point, it will exit with a status code of 1.
    If the script completes successfully, it will exit with a status code of 0.'''
    iam_username = os.environ.get('IAM_USERNAME', who_am_i())
    github_token = os.environ['PERSONAL_ACCESS_TOKEN']
    owner_repository = os.environ['OWNER_REPOSITORY']

    list_ret = iam.list_access_keys(UserName=iam_username)
    starting_num_keys = len(list_ret["AccessKeyMetadata"])

    # Check if two keys already exist, if so, exit 1
    if starting_num_keys >= 2:
        print("There are already 2 keys for this user. Cannot rotate tokens.")
        sys.exit(1)
    else:
        print(f"Validated <2 keys exist (current count: {starting_num_keys}), proceeding.")

    # generate new credentials
    (new_access_key, new_secret_key) = create_new_keys(iam_username)
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as fh:
        print(f'{access_key_name}={new_access_key}', file=fh)
        print(f'{secret_key_name}={new_secret_key}', file=fh)

    for repos in [x.strip() for x in owner_repository.split(',')]:
        # get repo pub key info
        (public_key, pub_key_id) = get_pub_key(repos, github_token)

        # encrypt the secrets
        encrypted_access_key = encrypt(public_key, new_access_key)
        encrypted_secret_key = encrypt(public_key, new_secret_key)

        # upload secrets
        upload_secret(repos, access_key_name, encrypted_access_key, pub_key_id, github_token)
        upload_secret(repos, secret_key_name, encrypted_secret_key, pub_key_id, github_token)

    # delete old keys
    if starting_num_keys == 1:
        delete_old_keys(iam_username, list_ret["AccessKeyMetadata"][0]["AccessKeyId"])

    sys.exit(0)


def who_am_i():
    '''Get the current IAM user.'''
    # ask the aws backend for myself with a boto3 sts client
    sts = boto3.client(
        'sts',
        aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
        aws_session_token=os.environ.get('AWS_SESSION_TOKEN')
    )

    user = sts.get_caller_identity()
    # return last element of splitted list to get username
    return user['Arn'].split("/")[-1]


def create_new_keys(iam_username):
    '''Create new access keys for the IAM user.'''
    # create the keys
    create_ret = iam.create_access_key(
            UserName=iam_username
        )

    new_access_key = create_ret['AccessKey']['AccessKeyId']
    new_secret_key = create_ret['AccessKey']['SecretAccessKey']

    # check to see if the keys were created
    second_list_ret = iam.list_access_keys(UserName=iam_username)
    access_keys = [k['AccessKeyId'] for k in second_list_ret["AccessKeyMetadata"]]

    if new_access_key not in access_keys:
        print("new keys failed to generate.")
        sys.exit(1)
    else:
        print("new keys generated, proceeding")
        return (new_access_key, new_secret_key)


def delete_old_keys(iam_username, current_access_id):
    '''Delete the old access keys for the IAM user.'''
    delete_ret = iam.delete_access_key(
            UserName=iam_username,
            AccessKeyId=current_access_id
        )

    if delete_ret['ResponseMetadata']['HTTPStatusCode'] != 200:
        print("deletion of original key failed")
        sys.exit(1)

# Update Actions Secret
# https://developer.github.com/v3/actions/secrets/#create-or-update-a-secret-for-a-repository
def encrypt(public_key: str, secret_value: str) -> str:
    """Encrypt a Unicode string using the public key."""
    public_key = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return b64encode(encrypted).decode("utf-8")


def get_pub_key(owner_repo, github_token):
    '''Get the public key for the GitHub repository. This is used to encrypt the
    new access keys before uploading them to the repository.'''
    # get public key for encrypting
    endpoint_path = 'repos' if "/" in owner_repo else 'orgs'
    endpoint = f'https://api.github.com/{endpoint_path}/{owner_repo}/actions/secrets/public-key'
    if environment_name := os.environ.get('GITHUB_ENVIRONMENT'):
        endpoint = f'https://api.github.com/{endpoint_path}/{owner_repo}/environments/{environment_name}/secrets/public-key'

    pub_key_ret = requests.get(
        endpoint,
        headers={'Authorization': f"Bearer {github_token}"}
    )

    if not pub_key_ret.status_code == requests.codes.ok:
        raise Exception(f"github public key request failed, \
                          status code: {pub_key_ret.status_code}, \
                          body: {pub_key_ret.text}, \
                          vars: {owner_repo} {github_token} \
                        ")

        sys.exit(1)

    # convert to json
    public_key_info = pub_key_ret.json()

    # extract values
    public_key = public_key_info['key']
    public_key_id = public_key_info['key_id']

    return (public_key, public_key_id)


def upload_secret(owner_repo, key_name, encrypted_value, pub_key_id, github_token):
    '''Upload the encrypted access key to the GitHub repository.'''
    # upload encrypted access key
    endpoint_path = 'repos' if "/" in owner_repo else 'orgs'
    endpoint = f'https://api.github.com/{endpoint_path}/{owner_repo}/actions/secrets/{key_name}'

    if environment_name := os.environ.get('GITHUB_ENVIRONMENT'):
        endpoint = f'https://api.github.com/{endpoint_path}/{owner_repo}/environments/{environment_name}/secrets/{key_name}'

    updated_secret = requests.put(
        endpoint,
        json={
            'encrypted_value': encrypted_value,
            'key_id': pub_key_id,
            # Todo: make this an input
            'visibility': 'private'
        },
        headers={'Authorization': f"Bearer {github_token}"}
    )
    # status codes github says are valid
    good_status_codes = [204, 201]

    if updated_secret.status_code not in good_status_codes:
        raise Exception(f"upload secret request failed, \
                          status code: {updated_secret.status_code}, \
                          body: {updated_secret.text}, \
                          vars: {owner_repo} {github_token} \
                        ")

        sys.exit(1)

    if environment_name := os.environ.get('GITHUB_ENVIRONMENT'):
        print(f'Updated: {key_name} in {owner_repo} for {environment_name}')
    else:
        print(f'Updated: {key_name} in {owner_repo}')


# run everything
main_function()
