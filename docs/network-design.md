# Level 1 Network Design Record

This is the "why" behind the VPC in `infra/stacks/level1_ec2_stack.py`. It is
written so the flow can be explained without opening the console.

## Address plan

| Range | Purpose |
| :--- | :--- |
| `10.20.0.0/16` | VPC |
| `10.20.0.0/24` | Public subnet A (AZ a) - ALB node, NAT gateway |
| `10.20.1.0/24` | Public subnet B (AZ b) - ALB node |
| `10.20.2.0/24` | Private subnet A (AZ a) - application instance |
| `10.20.3.0/24` | Private subnet B (AZ b) - reserved |

The VPC has DNS hostnames and DNS support enabled so the instance resolves
`bedrock-runtime.us-east-1.amazonaws.com` and friends through the VPC resolver
(`10.20.0.2`).

## Why the ALB can be public while the database is private

- The **ALB** is the only internet-facing component. It must be in public
  subnets to have an internet gateway route, and it is intentionally reachable
  on port 80 (see ADR 0004).
- The **instance** is in a private subnet: it has *no* route to the internet
  gateway. Its default route (`0.0.0.0/0`) points at the NAT gateway, so it can
  make outbound calls but cannot be reached inbound from the internet.
- **DynamoDB is not "private" because it sits in a subnet - it has no subnet at
  all.** It is a regional service reached through a gateway VPC endpoint that
  attaches to the private route tables. "Private" here means the request never
  leaves the AWS network and the table has no public endpoint or IP.

## Route tables

| Route table | Associated with | Routes |
| :--- | :--- | :--- |
| Public RT (per AZ) | Public subnets | `10.20.0.0/16` local, `0.0.0.0/0` → Internet Gateway |
| Private RT (per AZ) | Private subnets | `10.20.0.0/16` local, `0.0.0.0/0` → NAT Gateway, **prefix list → S3 gateway endpoint**, **prefix list → DynamoDB gateway endpoint** |

The two gateway endpoints inject prefix-list routes into the private route
tables. Traffic to S3 and DynamoDB therefore never touches the NAT gateway
(cost + a smaller egress path).

## Security group flows

| Source | Destination | Port | Notes |
| :--- | :--- | :--- | :--- |
| `0.0.0.0/0` | `AlbSecurityGroup` | 80 | Only open inbound port in the environment |
| `AlbSecurityGroup` | `AppSecurityGroup` | 8000 | **Security-group reference**, never a CIDR |
| `AppSecurityGroup` | `0.0.0.0/0` (via NAT) | 443, 80 | Bedrock, CloudWatch, SSM, and OS package repos |
| `AppSecurityGroup` | VPC resolver | 53 (udp) | DNS |
| `AlbSecurityGroup` | `AppSecurityGroup` | 8000 | ALB → target health checks and requests |

Both groups are created with `allow_all_outbound=False` and only the egress
rules above are added, so an accidental "allow all" is not the default posture.

### Security group vs. network ACL

This VPC uses the **default** network ACLs (allow all in/out) and relies on
security groups for enforcement. Security groups are **stateful** - a response
to an allowed inbound request is automatically allowed out - and can reference
other security groups. Network ACLs are **stateless** (return traffic needs its
own rule) and operate at the subnet boundary. For this lab, security groups give
the precise, reference-based control that matters, and the default NACL is
documented here rather than left as an accident.

## What changes in Level 2/3

- Level 2 keeps the same VPC shape but moves the workload to ECS tasks; the
  service security group then accepts traffic from the ALB group on 8000 for
  task ENIs instead of an instance ENI.
- Level 3 replaces the NAT gateway with interface VPC endpoints (or removes
  egress entirely) and adds VPC Flow Logs so the "which flow was blocked" question
  can be answered from data rather than inference.
